# WA2Corpus

Convert **WhatsApp chat exports** (`.zip` or `.txt`, from both **iOS** and **Android**) into **Sketch Engine**-ready XML corpora with out-of-the-box **Text Type** metadata filtering and clean tokenization boundaries.

## Why WA2Corpus?

Importing raw or naively converted WhatsApp chats into Sketch Engine causes several common corpus issues:

1. **Cross-message word gluing**: Most WhatsApp messages do not end with a period. If messages are not wrapped in Sketch Engine's native `<doc><p><s>` hierarchy with padded text nodes, Sketch Engine's XML-to-text extractor concatenates the last word of one message onto the first word of the next.
2. **Missing Text Type filters**: Sketch Engine's automatic XML compiler indexes attributes on `<doc>` as **Text Types**. Wrapping each message in `<doc>` makes `speaker`, `year`, `month`, `date`, `edited`, `has_media`, and `has_url` immediately available in Concordance, Wordlist, Keywords, and Trends filters without manual expert settings.
3. **Inline WhatsApp system & media noise**: WhatsApp injects invisible Left-to-Right Marks (`\u200e`) and appends inline notices directly inside real messages (e.g., image captions ending with `Bild weggelassen`, edited messages ending with `<This message was edited>`, or document filenames). `WA2Corpus` strips these notices across **EN, IT, DE, ES, FR, and PT** while recording `edited="true"` and `has_media="true"` as filterable metadata.
4. **Tokenization normalization**:
   - Strips URLs by default (recording `has_url="true"`) so random URL tokens (`utm_source`, tracking IDs) do not pollute wordlists.
   - Spaces emojis away from adjacent words, so words attached to emojis are not treated as distinct out-of-vocabulary tokens.
   - Normalizes curly apostrophes (`’` → `'`) and separates repeated punctuation glued between words (`dio....lo` → `dio.... lo`).

---

## Requirements

- **Python 3.9+** (uses only the Python standard library — no external dependencies required).

---

## Usage

### 1. Basic usage (auto-detects `.zip` or `.txt` in current directory)

```bash
python WA2Corpus.py
```

By default, speaker names have emojis and ASCII emoticons stripped automatically (e.g. `"ale :)"` → `"ale"`).

### 2. Rename speakers (`--rename` / `-r`)

Map raw or emoji-stripped WhatsApp display names to clean speaker labels:

```bash
python WA2Corpus.py --rename "ale=Ale" --rename "Milù=Pietro"
```

### 3. Anonymize speakers (`--anonymize`)

Automatically replace speaker names with `Speaker_1`, `Speaker_2`, etc. (any speakers explicitly mapped via `--rename` keep their custom label):

```bash
python WA2Corpus.py --anonymize
```

### 4. Specify input file(s) and output path

Pass one or more `.zip` or `.txt` exports (when multiple files are merged, a `source` attribute is added to each `<doc>`):

```bash
python WA2Corpus.py chat1.zip chat2.txt -o combined_corpus.xml
```

### 5. All CLI options

```text
usage: WA2Corpus.py [-h] [-o OUTPUT] [-r OLD=NEW] [--anonymize]
                    [--date-format {auto,dmy,mdy,ymd}]
                    [--keep-urls | --url-placeholder TOKEN]
                    [--strip-emojis | --keep-emojis]
                    [inputs ...]

options:
  inputs                Input .zip or .txt WhatsApp export file(s) or glob pattern(s).
                        Defaults to auto-detecting in the current directory.
  -o, --output OUTPUT   Output XML file path (default: corpus_sketch_engine.xml).
  -r, --rename OLD=NEW  Rename a speaker (repeatable), e.g. --rename "ale :)=Ale".
  --anonymize           Anonymize speakers not explicitly renamed via --rename as
                        Speaker_1, Speaker_2, etc.
  --date-format {auto,dmy,mdy,ymd}
                        Date component order in chat timestamps (default: auto).
  --keep-urls           Keep raw URLs in message text instead of stripping them.
  --url-placeholder TOKEN
                        Replace URLs with a placeholder token (e.g. "[URL]") instead of
                        stripping them.
  --strip-emojis        Remove all emojis from message text (default: keep emojis
                        separated by spaces).
  --keep-emojis         Keep emojis attached as-is without inserting spaces around them.
```

---

## Output XML Structure

Each valid message is emitted as a `<doc>` element containing a `<p>` paragraph and one `<s>` sentence tag per line:

```xml
<?xml version='1.0' encoding='utf-8'?>
<corpus id="whatsapp_chat" title="WhatsApp Chat">
  <doc id="p00001" speaker="Tizio" date="2025-02-19" month="2025-02" year="2025" edited="false" has_media="false" has_url="false">
    <p>
      <s> che carte fedeltà hai su klarna? </s>
    </p>
  </doc>
  <doc id="p00002" speaker="Caio" date="2025-02-19" month="2025-02" year="2025" edited="false" has_media="false" has_url="false">
    <p>
      <s> boh domani che riprendo il telefono le vedo </s>
    </p>
  </doc>
</corpus>
```

---

## Importing into Sketch Engine

1. In Sketch Engine, click **New corpus** → choose your language → select **I have my own texts**.
2. Upload the generated `corpus_sketch_engine.xml` file.
3. Click **Compile** (no manual XML structure configuration is needed).
4. In **Concordance**, **Wordlist**, **Keywords**, or **Trends**, open the **Text types** section to filter or group by:
   - `doc.speaker`
   - `doc.year`, `doc.month`, `doc.date`
   - `doc.edited`, `doc.has_media`, `doc.has_url`

---

## License

Released under the [MIT License](LICENSE).

