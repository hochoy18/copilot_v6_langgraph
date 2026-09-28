# i18n

`react-i18next` is bound by ADR-0029 §UI 语言 (default 中文, switcher to English).
The runtime wiring (`I18nextProvider` in `main.tsx`, `zh.json` + `en.json`
translation files, `<LanguageSwitcher />`) lands with the i18n ticket — *not*
in this scaffold, by intent: the two placeholder pages ship 中文 hard-coded so
the seam is visible, then refactor to `useTranslation()` when i18n lands.

US-25 in SPEC.md is the originating requirement.
