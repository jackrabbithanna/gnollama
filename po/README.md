# Translation review and maintenance

Reviewed against Gnollama 0.14.0 on 2026-09-10.

| Language | Locale | Translated current messages |
| --- | --- | --- |
| Arabic | `ar` | 715 / 715 |
| German | `de` | 715 / 715 |
| Greek | `el` | 715 / 715 |
| Spanish | `es` | 715 / 715 |
| French | `fr` | 715 / 715 |
| Hindi | `hi` | 715 / 715 |
| Italian | `it` | 715 / 715 |
| Japanese | `ja` | 715 / 715 |
| Korean | `ko` | 715 / 715 |
| Portuguese | `pt` | 715 / 715 |
| Swahili | `sw` | 715 / 715 |
| Turkish | `tr` | 715 / 715 |
| Ukrainian | `uk` | 715 / 715 |
| Simplified Chinese | `zh_CN` | 715 / 715 |

These counts include interface text, application-authored errors, store metadata,
and current release notes. English is the fallback for untranslated messages.
All fourteen catalogs were completed with AI assistance and checked for terminology,
placeholders, and completeness; the additions have not had independent
native-speaker review. Simplified Chinese is intended for written Mandarin;
Traditional Chinese is not included.

## Findings and changes

- **Ollama Cloud in 0.14.0.** All fourteen catalogs include cloud-host setup, API-key and keyring errors, session-only fallback, cloud limitations, and release notes. The template and catalog headers identify version 0.14.0.

- **Python translations could silently fall back to English.** The launcher
  configured C gettext and installed `_()` but did not configure Python's
  module-level gettext domain. Modules importing `gettext.gettext` therefore
  searched the default `messages` domain. The launcher now initializes both
  APIs before importing the application. Python documents these as separate
  [gettext configuration and installation APIs](https://docs.python.org/3/library/gettext.html).
- **Existing catalogs were stale.** Each had only 88 translations matching the
  then-current 688-message template. The catalogs now match current source,
  preserve reviewed translations, and cover all 715 messages. German, Spanish,
  French, and Portuguese now also translate the knowledge library, collections,
  embeddings, tools, URL imports, settings, validation errors, and release notes.
- **Extraction omitted visible text.** `widgets/message_list.py` was missing
  from `POTFILES.in`, excluding the empty-chat title and guidance. Extraction
  now includes every marked Python and UI source. GTK templates explicitly
  name the `gnollama` domain, including contextual shortcut labels.
- **Metadata could retain old translations after an incremental build.** The
  desktop and AppStream merge targets now depend on `LINGUAS` and every listed
  catalog, so edits rebuild the installed metadata as well as the runtime catalogs.
- **Longer labels could overflow narrow chat windows.** Tool and output controls
  now wrap, as do settings labels; the model-retention selector uses a vertical
  layout so its label and translated choices fit on smaller screens.
- **Translation errors and inconsistent terms were corrected.** French
  “Erreur de conexión” is now “Erreur de connexion”; Portuguese “Detalhes del
  modelo” is now “Detalhes do modelo”. French chat labels consistently use
  “discussion”; Spanish thinking controls use “razonamiento”. Portuguese
  wording is standardized toward European Portuguese (`pt`), using “guardar”,
  “eliminar”, “ligação”, and “gerir”. “Digest” is rendered as a cryptographic
  fingerprint/checksum, and parameter size as a parameter count, where
  appropriate. Menu mnemonics are distinct within each language.
- **Counts needed plural selection.** Image and character counts now use
  `ngettext`. Arabic has six forms, Hindi uses the singular category for
  integer counts 0 and 1, and Chinese has one form, following the integer
  categories in [Unicode CLDR's plural rules](https://www.unicode.org/cldr/charts/48/supplemental/language_plural_rules.html).
  Japanese and Korean also use one form. Italian and Greek use singular/plural;
  Turkish uses one/other categories, with the same wording for numeric image and
  character counts because Turkish nouns remain singular after numbers.
  Swahili uses singular/plural, including image-selection agreement. Ukrainian
  uses three integer forms: one, few, and many, with the teen exceptions checked
  separately from counts ending in 1 or 2–4.
  Other count summaries use label-style translations where needed.
- **Arabic needed layout support.** Application startup selects direction
  from a contextual translation, even without GTK's Arabic catalog. GTK's
  start/end alignment handles mirrored layout. JSON, code blocks, and server
  address entry retain left-to-right direction; plain document editors inherit
  the interface direction. The literal localhost URL is no longer translatable.
- **New-chat titles bypassed translation.** The database retains its English
  automatic-title marker, while tabs and sidebar rows translate it for display.
  Other stored titles retain their original text, and unchanged rename dialogs
  do not overwrite the marker with its translated display value.

## Selecting a language

The application follows the desktop language. To test a language explicitly,
close existing Gnollama windows and launch, for example:

```sh
LANGUAGE=zh_CN gnollama
LANGUAGE=hi gnollama
LANGUAGE=ar gnollama
LANGUAGE=it gnollama
LANGUAGE=el gnollama
LANGUAGE=tr gnollama
LANGUAGE=ja gnollama
LANGUAGE=ko gnollama
LANGUAGE=uk gnollama
LANGUAGE=sw gnollama
```

Use an installed UTF-8 desktop locale for `LANG`/`LC_ALL`; GTK suppresses
translations under `C` and `C.UTF-8`. `LANGUAGE` can override the message language
without requiring a generated locale for that language. Desktop language packs
and fonts provide translated GTK/portal controls and script coverage; application
catalogs do not replace them. Flatpak installations also need the corresponding
locale files to be installed.

## Updating translations

Install GNU gettext tools, then use the existing Meson build:

```sh
meson compile -C build gnollama-pot
meson compile -C build gnollama-update-po
meson compile -C build
meson test -C build --print-errorlogs
```

Review every fuzzy match before clearing its flag. Preserve format placeholders
such as `{0}`, `{1}`, and `{0:.4f}`, newline structure, shortcut contexts, and
menu mnemonic underscores. Keep API keys, model identifiers, command examples,
and URLs intact. The contextual `ltr` entry must be `rtl` only for right-to-left
languages. Register new locales in sorted `LINGUAS`; add newly translatable
sources to sorted `POTFILES.in`.

The regression suite compiles every catalog with `msgfmt --check`, verifies
extraction and complete translations for every supported language, tests plural
categories and mnemonic uniqueness, and launches separate processes to check
Python/C gettext, fallbacks, GTK template/context translation, text direction,
and narrow-window control widths. GTK checks require a display; other catalog
checks can run independently:

```sh
python3 -m unittest discover -s tests -p test_i18n.py -v
```

Validation on 2026-09-10: the GNOME 50 SDK build, desktop/AppStream/schema checks,
and all 144 regression tests passed with a virtual display and no skipped tests.
A catalog timestamp change rebuilt both metadata targets, and the merged
AppStream text matched the catalogs in every language.
A staged installation under `/tmp` included all fourteen compiled catalogs. Its
chat and settings views were also inspected in all fourteen languages at desktop
and narrow widths, including Arabic mirroring and script rendering.

## Remaining scope

Native-speaker review of the completed catalogs remains useful for idiom and local
technical vocabulary. Catalog completeness is not a linguistic certification.

Raw Ollama responses, model metadata values, JSON/code, and third-party exception
details retain their original language. Numeric API inputs still accept the
existing dot-decimal syntax, and timestamps retain the existing ISO-style
format. The interface language does not force a model's response language or
guarantee multilingual retrieval quality; that depends on the chosen models.
