#!/usr/bin/env python3
"""
WA2Corpus — Convert WhatsApp chat exports (.zip or .txt, iOS & Android)
into Sketch Engine-compatible XML corpora with Text Type metadata and
clean sentence/token boundaries.
"""

import argparse
import glob
import os
import re
import sys
import unicodedata
import xml.etree.ElementTree as ET
import zipfile
from datetime import datetime

# Ensure standard output can handle UTF-8 on Windows
if sys.stdout and hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


def strip_invalid_xml_chars(text):
    """Filter out characters that are invalid in XML 1.0."""
    return re.sub(
        r"[^\x09\x0A\x0D\x20-\uD7FF\uE000-\uFFFD\U00010000-\U0010FFFF]",
        "",
        text,
    )


def clean_control_chars(text):
    """Strip directional, zero-width, and non-breaking space control characters (excluding LRM \\u200e)."""
    return re.sub(r"[\u200f\u202a-\u202e\u2066-\u2069\ufeff\xa0\u202f]", " ", text)


# Matches both iOS and Android WhatsApp exports:
# iOS:     [DD.MM.YY, HH:MM:SS] Speaker Name: Message
# Android: DD/MM/YYYY, HH:MM - Speaker Name: Message
# Supports optional AM/PM (including a.m./p.m.) and 2- or 4-digit years.
MESSAGE_LINE_PATTERN = re.compile(
    r"^\u200e*(?:"
    r"\[(\d{1,4}[./-]\d{1,2}[./-]\d{1,4}),?\s*(\d{1,2}:\d{2}(?::\d{2})?(?:\s*[APap]\.?\s*[Mm]\.?)?)\]\s*(.+?):\s+(.*)"
    r"|"
    r"(\d{1,4}[./-]\d{1,2}[./-]\d{1,4}),?\s*(\d{1,2}:\d{2}(?::\d{2})?(?:\s*[APap]\.?\s*[Mm]\.?)?)\s*-\s*(.+?):\s+(.*)"
    r")$"
)

# Matches timestamped system lines without a speaker (common on Android, e.g. "12/05/2023, 10:00 - Messages and calls are end-to-end encrypted...")
SYSTEM_LINE_NO_SPEAKER_PATTERN = re.compile(
    r"^\u200e*(?:"
    r"\[\d{1,4}[./-]\d{1,2}[./-]\d{1,4},?\s*\d{1,2}:\d{2}(?::\d{2})?(?:\s*[APap]\.?\s*[Mm]\.?)?\]\s*(?!.+?:\s)"
    r"|"
    r"\d{1,4}[./-]\d{1,2}[./-]\d{1,4},?\s*\d{1,2}:\d{2}(?::\d{2})?(?:\s*[APap]\.?\s*[Mm]\.?)?\s*-\s*(?![^:]+?:\s)"
    r").*$"
)

# Emoji / pictograph sequence pattern
EMOJI_SEQ_PATTERN = re.compile(
    r"(?:[\U0001F1E6-\U0001F1FF]{2}|"
    r"[\U0001F300-\U0001FAFF\u2600-\u27BF\u2300-\u23FF\u2B50\u2B55\u203C\u2049\u2122\u2139\u24C2\u3030\u303D\u3297\u3299]"
    r"(?:[\uFE0E\uFE0F\U0001F3FB-\U0001F3FF])?"
    r"(?:\u200D[\U0001F300-\U0001FAFF\u2600-\u27BF\u2300-\u23FF](?:[\uFE0E\uFE0F\U0001F3FB-\U0001F3FF])?)*)+"
)

# ASCII emoticons commonly found in contact display names
ASCII_EMOTICON_PATTERN = re.compile(
    r"(?:(?<=^)|(?<=\s))(?::|;|=|-)?(?:\)+|\(+|D+|P+|p+|\]+|\[+|<3+|\^+\_\^+|\^\^)(?=\s|$)"
)


def default_clean_speaker(raw_speaker):
    """Strip LRM, emojis, and trailing/leading ASCII emoticons from a speaker name."""
    cleaned = raw_speaker.replace("\u200e", "").strip()
    cleaned = EMOJI_SEQ_PATTERN.sub("", cleaned)
    cleaned = ASCII_EMOTICON_PATTERN.sub("", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned or raw_speaker.replace("\u200e", "").strip()


def build_speaker_normalizer(rename_pairs=None, anonymize=False):
    """
    Create a stateful speaker normalization function supporting:
      - Custom rename mappings (--rename "Old=New")
      - Automatic emoji/emoticon stripping by default
      - Optional speaker anonymization (--anonymize -> Speaker_1, Speaker_2, ...)
    """
    custom_map = {}
    if rename_pairs:
        for pair in rename_pairs:
            if "=" not in pair:
                raise ValueError(f"Invalid --rename value '{pair}'. Expected format 'OldName=NewName'.")
            old_name, new_name = pair.split("=", 1)
            old_norm = unicodedata.normalize("NFC", old_name.strip()).lower()
            custom_map[old_norm] = new_name.strip()

    anon_map = {}

    def normalize(raw_speaker):
        raw_clean = raw_speaker.replace("\u200e", "").strip()
        raw_key = unicodedata.normalize("NFC", raw_clean).lower()
        stripped = default_clean_speaker(raw_clean)
        stripped_key = unicodedata.normalize("NFC", stripped).lower()

        if raw_key in custom_map:
            return custom_map[raw_key]
        if stripped_key in custom_map:
            return custom_map[stripped_key]

        if anonymize:
            if stripped_key not in anon_map:
                anon_map[stripped_key] = f"Speaker_{len(anon_map) + 1}"
            return anon_map[stripped_key]

        return stripped

    return normalize


# Whole-message system/omitted/call patterns (EN, IT, DE, ES, FR, PT + Android)
WHOLE_DROP_PATTERNS = [
    # Android & generic angle-bracket media omissions
    re.compile(
        r"^\u200e*<(?:media omitted|media omessi|medien ausgeschlossen|multimedia omitido|médias omis|mídia oculta)>\.?$",
        re.IGNORECASE,
    ),
    # Document / file attachment lines without caption:
    # e.g. "file.pdf • 2 Seiten Dokument weggelassen" or Android "IMG-2023.jpg (file attached)"
    re.compile(
        r"^.*?\u200e*(?:"
        r"dokument\s+weggelassen|documento\s+omesso|document\s+omitted|"
        r"documento\s+omitido|document\s+omis|documento\s+ocultado"
        r")\.?$",
        re.IGNORECASE,
    ),
    re.compile(
        r"^\u200e*[^\n]+?\s*\((?:"
        r"file attached|datei angehängt|file allegato|archivo adjunto|fichier joint|arquivo anexado"
        r")\.?\)\s*$",
        re.IGNORECASE,
    ),
    # Standalone media omissions (DE, IT, EN, ES, FR, PT)
    re.compile(
        r"^\u200e*(?:audio|bild|video|sticker|dokument|gif|kontaktkarte|standort|videonotiz)\s+(?:weggelassen|ausgelassen)\.?$",
        re.IGNORECASE,
    ),
    re.compile(
        r"^\u200e*(?:audio|immagine|foto|video|sticker|documento|contatto|scheda contatto|posizione|gif|nota video|messaggio vocale)\s+omess[oaie]\.?$",
        re.IGNORECASE,
    ),
    re.compile(
        r"^\u200e*(?:image|audio|video|sticker|document|contact card|location|gif|video note|voice message)\s+omitted\.?$",
        re.IGNORECASE,
    ),
    re.compile(
        r"^\u200e*(?:imagen|audio|video|vídeo|sticker|documento|tarjeta de contacto|ubicación|gif|nota de video)\s+omitid[oa]\.?$",
        re.IGNORECASE,
    ),
    re.compile(
        r"^\u200e*(?:image|audio|vidéo|sticker|document|fiche contact|localisation|gif|note vidéo)\s+omis[e]?\.?$",
        re.IGNORECASE,
    ),
    re.compile(
        r"^\u200e*(?:imagem|áudio|vídeo|figurinha|documento|cartão de contato|localização|gif|recado de vídeo)\s+ocultad[oa]\.?$",
        re.IGNORECASE,
    ),
    # Deleted messages (DE, IT, EN, ES, FR, PT)
    re.compile(
        r"^\u200e*(?:"
        r"diese nachricht wurde gelöscht|du hast diese nachricht gelöscht|"
        r"hai eliminato questo messaggio|questo messaggio è stato eliminato|"
        r"this message was deleted|you deleted this message|"
        r"eliminaste este mensaje|este mensaje fue eliminado|se eliminó este mensaje|"
        r"vous avez supprimé ce message|ce message a été supprimé|"
        r"você apagou esta mensagem|esta mensagem foi apagada"
        r")\.?$",
        re.IGNORECASE,
    ),
    # Voice/video calls (DE, IT, EN, ES, FR, PT)
    re.compile(
        r"^\u200e*(?:"
        r"verpasster\s+(?:gruppen-?)?sprachanruf|verpasster\s+videoanruf|verpasster\s+gruppenanruf|sprachanruf|videoanruf|gruppenanruf|anruf\s+fehlgeschlagen|"
        r"chiamata\s+(?:vocale\s+)?persa|videochiamata\s+persa|chiamata\s+vocale|videochiamata|"
        r"missed\s+(?:group\s+)?voice\s+call|missed\s+(?:group\s+)?video\s+call|voice\s+call|video\s+call|"
        r"llamada\s+perdida|videollamada\s+perdida|llamada\s+de\s+voz|videollamada|"
        r"appel\s+(?:vocal\s+|vidéo\s+)?manqué|appel\s+vocal|appel\s+vidéo|"
        r"chamada\s+(?:de\s+voz\s+|de\s+vídeo\s+)?perdida|chamada\s+de\s+voz|chamada\s+de\s+vídeo"
        r")\b",
        re.IGNORECASE,
    ),
    # System & encryption notices (DE, IT, EN, ES, FR, PT)
    re.compile(r"ende-zu-ende-verschlüsselt", re.IGNORECASE),
    re.compile(r"nachrichtendauer aktualisiert", re.IGNORECASE),
    re.compile(r"selbstlöschende nachrichten", re.IGNORECASE),
    re.compile(r"sicherheitsnummer hat sich geändert", re.IGNORECASE),
    re.compile(r"sticker-set wird nicht exportiert", re.IGNORECASE),
    re.compile(r"end-to-end encrypted", re.IGNORECASE),
    re.compile(r"disappearing messages", re.IGNORECASE),
    re.compile(r"security code changed", re.IGNORECASE),
    re.compile(r"crittografia end-to-end", re.IGNORECASE),
    re.compile(r"messaggi effimeri", re.IGNORECASE),
    re.compile(r"codice di sicurezza.*cambiato", re.IGNORECASE),
    re.compile(r"cifrados de extremo a extremo", re.IGNORECASE),
    re.compile(r"chiffrés de bout en bout", re.IGNORECASE),
    re.compile(r"criptografadas de ponta a ponta", re.IGNORECASE),
]

# Inline edited notice patterns (DE, IT, EN, ES, FR, PT)
EDITED_PATTERN = re.compile(
    r"\u200e*<(?:"
    r"diese nachricht wurde bearbeitet|"
    r"questo messaggio è stato modificato|"
    r"this message was edited|"
    r"se editó este mensaje|este mensaje fue editado|"
    r"ce message a été modifié|"
    r"esta mensagem foi editada"
    r")\.?>",
    re.IGNORECASE,
)

# Inline media notice patterns (e.g. captions on images/videos/locations or Android file attachments with captions)
INLINE_MEDIA_PATTERNS = [
    re.compile(
        r"(?:•\s*\u200e*\d+\s+(?:seiten?|pages?|pagin[ae]|páginas?)\s*)?\u200e*(?:"
        r"(?:audio|bild|video|sticker|dokument|gif|kontaktkarte|videonotiz)\s+(?:weggelassen|ausgelassen)|"
        r"(?:audio|immagine|foto|video|sticker|documento|contatto|scheda contatto|posizione|gif|nota video|messaggio vocale)\s+omess[oaie]|"
        r"(?:image|audio|video|sticker|document|contact card|location|gif|video note|voice message)\s+omitted|"
        r"(?:imagen|audio|video|vídeo|sticker|documento|tarjeta de contacto|ubicación|gif|nota de video)\s+omitid[oa]|"
        r"(?:image|audio|vidéo|sticker|document|fiche contact|localisation|gif|note vidéo)\s+omis[e]?|"
        r"(?:imagem|áudio|vídeo|figurinha|documento|cartão de contato|localização|gif|recado de vídeo)\s+ocultad[oa]"
        r")\.?",
        re.IGNORECASE,
    ),
    re.compile(
        r"\u200e*(?:standort|posizione|location|ubicación|localisation|localização):\s*(?:https?://\S+)?",
        re.IGNORECASE,
    ),
    re.compile(
        r"^\u200e*[^\n]+?\s*\((?:file attached|datei angehängt|file allegato|archivo adjunto|fichier joint|arquivo anexado)\)\s*\n",
        re.IGNORECASE,
    ),
]

# Poll patterns (DE, IT, EN, ES, FR, PT)
POLL_HEADER_PATTERN = re.compile(
    r"^\u200e*(?:ABSTIMMUNG|SONDAGGIO|POLL|ENCUESTA|SONDAGE|ENQUETE):\s*$",
    re.IGNORECASE | re.MULTILINE,
)
POLL_OPTION_PATTERN = re.compile(
    r"^\u200e*(?:OPTION|OPZIONE|OPCIÓN|OPÇÃO):\s*(.*?)(?:\s*\(\d+\s*(?:Stimme[n]?|vot[oi]|votes?|votos?|voix)\))?\s*$",
    re.IGNORECASE | re.MULTILINE,
)

# URL pattern
URL_PATTERN = re.compile(r"(?:https?://|www\.)\S+", re.IGNORECASE)


def clean_and_normalize_message(
    raw_content,
    url_mode="strip",
    url_placeholder="[URL]",
    emoji_mode="space",
):
    """
    Clean a single message's raw content, extracting metadata flags (edited, has_media, has_url)
    and normalizing text for Sketch Engine tokenization.
    Returns (cleaned_lines, edited, has_media, has_url) or (None, ...) if message should be dropped.
    """
    text = strip_invalid_xml_chars(clean_control_chars(raw_content)).strip()
    if not text:
        return None, False, False, False

    # Check if the entire message is a system/omitted/deleted/call notice
    first_line = text.splitlines()[0].strip()
    for p in WHOLE_DROP_PATTERNS:
        if p.search(text) or p.search(first_line):
            return None, False, False, False

    edited = False
    has_media = False
    has_url = False

    # Strip inline edited markers
    if EDITED_PATTERN.search(text):
        edited = True
        text = EDITED_PATTERN.sub(" ", text)

    # Strip inline media notices (e.g. captions with 'Bild weggelassen' or 'Standort: ...')
    for mp in INLINE_MEDIA_PATTERNS:
        if mp.search(text):
            has_media = True
            text = mp.sub(" ", text)

    # Clean poll syntax if present
    if POLL_HEADER_PATTERN.search(text):
        text = POLL_HEADER_PATTERN.sub("", text)
        text = POLL_OPTION_PATTERN.sub(r"\1", text)

    # Strip remaining LRM markers
    text = text.replace("\u200e", " ")

    # Detect and handle URLs
    if URL_PATTERN.search(text):
        has_url = True
        if url_mode == "strip":
            text = URL_PATTERN.sub(" ", text)
        elif url_mode == "placeholder":
            text = URL_PATTERN.sub(f" {url_placeholder} ", text)
        else:
            # keep-urls: still ensure whitespace around URLs so they don't glue to adjacent words
            text = URL_PATTERN.sub(r" \g<0> ", text)

    # Normalize curly apostrophes and single-quote variants to straight ASCII apostrophe (')
    text = re.sub(r"[\u2018\u2019\u02BC\u2032`´]", "'", text)

    # Normalize curly double quotes
    text = re.sub(r"[\u201C\u201D\u00AB\u00BB]", '"', text)

    # Separate repeated punctuation (e.g. 'dio....lo', 'quale???e', 'cosa?!ma') glued between words
    text = re.sub(r"(\.{2,}|[?!]+)(?=[^\W_])", r"\1 ", text)

    # Handle emojis
    if emoji_mode == "strip":
        text = EMOJI_SEQ_PATTERN.sub(" ", text)
    elif emoji_mode == "space":
        text = EMOJI_SEQ_PATTERN.sub(r" \g<0> ", text)

    # Clean up each line individually
    cleaned_lines = []
    for line in text.splitlines():
        line = re.sub(r"[ \t]+", " ", line).strip()
        if line:
            cleaned_lines.append(line)

    if not cleaned_lines:
        return None, edited, has_media, has_url

    return cleaned_lines, edited, has_media, has_url


def detect_date_order(date_strings):
    """
    Auto-detect date component order ('dmy', 'mdy', or 'ymd') across a collection of date strings.
    Defaults to 'dmy' if all components are <= 12.
    """
    for ds in date_strings:
        parts = re.split(r"[./-]", ds.strip())
        if len(parts) != 3:
            continue
        try:
            p0, p1, p2 = int(parts[0]), int(parts[1]), int(parts[2])
        except ValueError:
            continue

        if len(parts[0]) == 4 or p0 > 31:
            return "ymd"
        if p0 > 12 and p1 <= 12:
            return "dmy"
        if p1 > 12 and p0 <= 12:
            return "mdy"

    return "dmy"


def parse_datetime(date_str, time_str, date_order="dmy"):
    """Parse date and time strings into (YYYY-MM-DD, YYYY-MM, YYYY)."""
    parts = re.split(r"[./-]", date_str.strip())
    if len(parts) != 3:
        return None, None, None

    if date_order == "ymd":
        y, m, d = parts
    elif date_order == "mdy":
        m, d, y = parts
    else:
        d, m, y = parts

    if len(y) == 2:
        y = "20" + y

    # Normalize time (handle optional AM/PM)
    t_clean = re.sub(r"\s+", " ", time_str.strip()).upper()
    is_pm = "PM" in t_clean or "P.M." in t_clean or "P. M." in t_clean
    is_am = "AM" in t_clean or "A.M." in t_clean or "A. M." in t_clean
    t_digits = re.sub(r"[^0-9:]", "", t_clean)
    time_parts = t_digits.split(":")
    if len(time_parts) == 2:
        t_digits += ":00"
        time_parts.append("00")

    try:
        hour = int(time_parts[0])
        if is_pm and hour < 12:
            hour += 12
        elif is_am and hour == 12:
            hour = 0
        dt = datetime(int(y), int(m), int(d), hour, int(time_parts[1]), int(time_parts[2]))
        return dt.strftime("%Y-%m-%d"), dt.strftime("%Y-%m"), dt.strftime("%Y")
    except ValueError:
        try:
            day_fmt = f"{int(y):04d}-{int(m):02d}-{int(d):02d}"
            month_fmt = f"{int(y):04d}-{int(m):02d}"
            return day_fmt, month_fmt, f"{int(y):04d}"
        except ValueError:
            return None, None, None


IGNORED_TXT_NAMES = {"requirements.txt", "cmakelists.txt", "license.txt", "readme.txt"}


def resolve_input_files(user_inputs=None):
    """Resolve one or more input files (.zip or .txt), handling globs and Unicode NFC/NFD normalization."""
    resolved = []

    if user_inputs:
        for pat in user_inputs:
            matched = []
            for variant in (pat, unicodedata.normalize("NFC", pat), unicodedata.normalize("NFD", pat)):
                if os.path.exists(variant):
                    matched.append(os.path.abspath(variant))
                    break
                glob_hits = glob.glob(variant)
                if glob_hits:
                    matched.extend(os.path.abspath(h) for h in glob_hits)
                    break
            resolved.extend(matched)
    else:
        # Auto-detect .zip or .txt in current working directory (or script directory)
        for search_dir in (os.getcwd(), os.path.dirname(os.path.abspath(__file__))):
            for ext in ("*.zip", "*.txt"):
                for match in sorted(glob.glob(os.path.join(search_dir, ext))):
                    if os.path.basename(match).lower() not in IGNORED_TXT_NAMES:
                        resolved.append(os.path.abspath(match))
            if resolved:
                break

    # Deduplicate while preserving order
    seen = set()
    unique_files = []
    for path in resolved:
        norm_key = os.path.normcase(unicodedata.normalize("NFC", path))
        if norm_key not in seen:
            seen.add(norm_key)
            unique_files.append(path)

    return unique_files


def extract_raw_text(file_path):
    """Extract WhatsApp chat text from a .zip archive or plain .txt file."""
    if file_path.lower().endswith(".zip"):
        with zipfile.ZipFile(file_path, "r") as zf:
            names = zf.namelist()
            chat_file = next((n for n in names if n.lower().endswith(".txt")), None)
            if not chat_file:
                raise FileNotFoundError(f"No .txt file found inside {file_path}")
            raw_bytes = zf.read(chat_file)
            return raw_bytes.decode("utf-8-sig", errors="replace")
    else:
        with open(file_path, "r", encoding="utf-8-sig", errors="replace") as f:
            return f.read()


def parse_whatsapp(
    raw_text,
    speaker_normalizer=None,
    date_format="auto",
    url_mode="strip",
    url_placeholder="[URL]",
    emoji_mode="space",
    source_name=None,
):
    """Parse raw WhatsApp export text into structured, normalized message dicts."""
    if speaker_normalizer is None:
        speaker_normalizer = build_speaker_normalizer()

    raw_entries = []
    current_entry = None

    for raw_line in raw_text.splitlines():
        line = clean_control_chars(raw_line).strip()
        if not line:
            continue

        match = MESSAGE_LINE_PATTERN.match(line)
        if match:
            if current_entry:
                raw_entries.append(current_entry)
            g = match.groups()
            if g[0] is not None:
                date_part, time_part, speaker, content = g[0], g[1], g[2], g[3]
            else:
                date_part, time_part, speaker, content = g[4], g[5], g[6], g[7]

            current_entry = {
                "date_raw": date_part,
                "time_raw": time_part,
                "speaker_raw": speaker,
                "content": content.strip(),
            }
        elif SYSTEM_LINE_NO_SPEAKER_PATTERN.match(line):
            # Timestamped system notice without speaker (e.g. Android encryption notice) — skip
            continue
        else:
            # Continuation of previous message (multiline)
            if current_entry:
                current_entry["content"] += "\n" + line

    if current_entry:
        raw_entries.append(current_entry)

    resolved_order = (
        detect_date_order(e["date_raw"] for e in raw_entries)
        if date_format == "auto"
        else date_format
    )

    filtered = []
    for entry in raw_entries:
        cleaned_lines, edited, has_media, has_url = clean_and_normalize_message(
            entry["content"],
            url_mode=url_mode,
            url_placeholder=url_placeholder,
            emoji_mode=emoji_mode,
        )
        if not cleaned_lines:
            continue

        day_str, month_str, year_str = parse_datetime(
            entry["date_raw"], entry["time_raw"], date_order=resolved_order
        )
        msg = {
            "speaker": speaker_normalizer(entry["speaker_raw"]),
            "date": day_str,
            "month": month_str,
            "year": year_str,
            "lines": cleaned_lines,
            "content": "\n".join(cleaned_lines),
            "edited": edited,
            "has_media": has_media,
            "has_url": has_url,
        }
        if source_name:
            msg["source"] = source_name
        filtered.append(msg)

    return filtered


def build_sketch_engine_xml(messages, corpus_id="whatsapp_chat", title="WhatsApp Chat"):
    """
    Build native Sketch Engine XML using standard <doc>, <p>, and <s> structures
    so Text Types filtering (speaker, year, month, date, edited, has_media, has_url)
    and sentence/token boundaries work out-of-the-box in Sketch Engine.
    """
    root = ET.Element("corpus", {"id": corpus_id, "title": title})

    for idx, msg in enumerate(messages, start=1):
        doc_attrs = {
            "id": f"p{idx:05d}",
            "speaker": msg["speaker"],
        }
        if msg.get("date"):
            doc_attrs["date"] = msg["date"]
        if msg.get("month"):
            doc_attrs["month"] = msg["month"]
        if msg.get("year"):
            doc_attrs["year"] = msg["year"]
        if msg.get("source"):
            doc_attrs["source"] = msg["source"]

        doc_attrs["edited"] = "true" if msg.get("edited") else "false"
        doc_attrs["has_media"] = "true" if msg.get("has_media") else "false"
        doc_attrs["has_url"] = "true" if msg.get("has_url") else "false"

        doc_el = ET.SubElement(root, "doc", doc_attrs)
        p_el = ET.SubElement(doc_el, "p")

        lines = msg.get("lines") or [msg["content"]]
        for line in lines:
            s_el = ET.SubElement(p_el, "s")
            # Leading and trailing spaces inside <s> ensure Sketch Engine's XML-to-text
            # extractor never concatenates tokens across <s> or <doc> boundaries.
            s_el.text = f" {line} "

    ET.indent(root, space="  ")
    return ET.tostring(root, encoding="utf-8", xml_declaration=True)


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description=(
            "Convert WhatsApp chat exports (.zip or .txt, iOS & Android) into "
            "Sketch Engine XML corpora with Text Type metadata and clean sentence boundaries."
        )
    )
    parser.add_argument(
        "inputs",
        nargs="*",
        help="Input .zip or .txt WhatsApp export file(s) or glob pattern(s). Defaults to auto-detecting in the current directory.",
    )
    parser.add_argument(
        "-o",
        "--output",
        default="corpus_sketch_engine.xml",
        help="Output XML file path (default: corpus_sketch_engine.xml).",
    )
    parser.add_argument(
        "-r",
        "--rename",
        action="append",
        default=[],
        metavar="OLD=NEW",
        help='Rename a speaker (repeatable), e.g. --rename "ale :)=Ale" --rename "Milù 🐈=Pietro".',
    )
    parser.add_argument(
        "--anonymize",
        action="store_true",
        help="Anonymize speakers not explicitly renamed via --rename as Speaker_1, Speaker_2, etc.",
    )
    parser.add_argument(
        "--date-format",
        choices=["auto", "dmy", "mdy", "ymd"],
        default="auto",
        help="Date component order in chat timestamps (default: auto).",
    )

    url_group = parser.add_mutually_exclusive_group()
    url_group.add_argument(
        "--keep-urls",
        action="store_true",
        help="Keep raw URLs in message text instead of stripping them.",
    )
    url_group.add_argument(
        "--url-placeholder",
        metavar="TOKEN",
        help='Replace URLs with a placeholder token (e.g. "[URL]") instead of stripping them.',
    )

    emoji_group = parser.add_mutually_exclusive_group()
    emoji_group.add_argument(
        "--strip-emojis",
        action="store_true",
        help="Remove all emojis from message text (default: keep emojis separated by spaces).",
    )
    emoji_group.add_argument(
        "--keep-emojis",
        action="store_true",
        help="Keep emojis attached as-is without inserting spaces around them.",
    )

    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)

    target_files = resolve_input_files(args.inputs)
    if not target_files:
        print(
            "Error: No WhatsApp export (.zip or .txt) found. Pass a file path or place your export in the current directory.",
            file=sys.stderr,
        )
        sys.exit(1)

    try:
        speaker_normalizer = build_speaker_normalizer(
            rename_pairs=args.rename, anonymize=args.anonymize
        )
    except ValueError as err:
        print(f"Error: {err}", file=sys.stderr)
        sys.exit(1)

    if args.keep_urls:
        url_mode = "keep"
        url_placeholder = "[URL]"
    elif args.url_placeholder:
        url_mode = "placeholder"
        url_placeholder = args.url_placeholder
    else:
        url_mode = "strip"
        url_placeholder = "[URL]"

    if args.strip_emojis:
        emoji_mode = "strip"
    elif args.keep_emojis:
        emoji_mode = "keep"
    else:
        emoji_mode = "space"

    all_messages = []
    multiple_files = len(target_files) > 1

    for target_file in target_files:
        print(f"Loading chat from: {target_file}")
        raw_text = extract_raw_text(target_file)
        base_name = os.path.splitext(os.path.basename(target_file))[0]
        msgs = parse_whatsapp(
            raw_text,
            speaker_normalizer=speaker_normalizer,
            date_format=args.date_format,
            url_mode=url_mode,
            url_placeholder=url_placeholder,
            emoji_mode=emoji_mode,
            source_name=base_name if multiple_files else None,
        )
        all_messages.extend(msgs)

    speakers = sorted(set(m["speaker"] for m in all_messages))
    print(
        f"Parsed {len(all_messages)} valid messages from {len(speakers)} speaker(s): {', '.join(speakers)}"
    )

    if multiple_files:
        title = "WhatsApp Corpus"
        corpus_id = "whatsapp_corpus"
    else:
        title = os.path.splitext(os.path.basename(target_files[0]))[0]
        corpus_id = (
            re.sub(r"\W+", "_", unicodedata.normalize("NFKD", title)).strip("_").lower()
            or "whatsapp_corpus"
        )

    xml_data = build_sketch_engine_xml(all_messages, corpus_id=corpus_id, title=title)

    output_file = os.path.abspath(args.output)
    with open(output_file, "wb") as f:
        f.write(xml_data)

    print(f"Successfully generated Sketch Engine XML at: {output_file}")


if __name__ == "__main__":
    main()
