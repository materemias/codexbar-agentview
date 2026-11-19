"""stretch_text.short_forms against the parse cases of omp-stretch's
stretch-core.test.ts, each drawn as a closed view: a link there is its short
form here, and a stretch still being written shows its short form."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "contents" / "scripts"))
from stretch_text import short_forms  # noqa: E402


class ShortFormTests(unittest.TestCase):
    def check(self, cases: dict[str, str]) -> None:
        for text, shown in cases.items():
            with self.subTest(text=text):
                self.assertEqual(short_forms(text), shown)

    def test_nested_stretches_show_the_outer_short_form(self) -> None:
        self.check({"A {{cache|stale {{cache key|cache key from `package.json`}} cache}} here.": "A cache here."})

    def test_braces_inside_code_never_open_a_stretch(self) -> None:
        text = "Use `{{a|b}}` and\n```\n{{x|y}}\n```\n"
        self.check({text: text, "~~~\n{{x|y}}\n~~~\n{{a|b}}": "~~~\n{{x|y}}\n~~~\na"})

    def test_a_backtick_without_its_closing_run_is_text(self) -> None:
        self.check({
            "Press the ` key. {{Cache fails.|The cache fails often.}}": "Press the ` key. Cache fails.",
            "``a ``` b`` {{short|long}}": "``a ``` b`` short",
        })

    def test_a_long_form_may_close_on_its_code_fences_closing_line(self) -> None:
        self.check({
            "{{Run.|Run:\n```sh\nmake\n```}} after": "Run. after",
            # Drawn open, the text after it starts its own line.
            "{{|Run:\n```sh\nmake\n```}} after": "Run:\n```sh\nmake\n```\n after",
        })

    def test_placeholders_stay_as_written(self) -> None:
        self.check({
            "{{name}}": "{{name}}", "{{}}": "{{}}", "{{ user.name }}": "{{ user.name }}",
            "{{#if done}}": "{{#if done}}", "{{name.}}": "{{name.}}", "{{a b}}": "{{a b}}",
        })

    def test_a_sentence_written_without_its_short_form_opens_from_the_words_before_it(self) -> None:
        self.check({
            "2. No automatic backup {{(the `hindsight-data` volume).}}\n3. Not checked. {{My test reply went into a thread.}}":
                "2. No automatic backup\n3. Not checked.",
            "Done. {{It took a minute.}}": "Done.",
        })

    def test_placeholders_inside_a_long_form_stay_hidden_with_it(self) -> None:
        self.check({"A {{short|short long {{name}} and {{nested|nested detail}}}}.": "A short."})

    def test_a_gist_written_before_its_own_stretch_as_well_shows_once(self) -> None:
        self.check({
            "- **AMD is out.** {{AMD is out.|AMD is out: its data starts in 2021.}}": "- AMD is out.",
            "Checks pass. Checks pass. {{Checks pass.|Checks pass, all 9.}}": "Checks pass. Checks pass.",
            # Mid-sentence words that happen to match the short form stay.
            "on a stale cache {{cache|cache keyed by hash}}": "on a stale cache cache",
            "{{Use|Use it}} cache {{cache|cache keyed by hash}}": "Use cache cache",
            # Code is never cut.
            "`let x = 1; x` {{x|x is returned}}": "`let x = 1; x` x",
            # Emphasis that closes after the stretch keeps both marks.
            "**Checks pass. {{Checks pass.|Checks pass in CI.}}**": "**Checks pass. Checks pass.**",
            "config_path. {{config_path.|config_path. holds the default.}}": "config_path.",
            "_Checks pass. {{Checks pass.|Checks pass in CI.}}_": "_Checks pass. Checks pass._",
            # Markdown marks around the copy go with it.
            "**Bold** {{**Bold**|Bolder}}": "**Bold**",
        })

    def test_a_long_form_that_rewrites_its_short_form_still_shows_the_short_form(self) -> None:
        self.check({
            "- Rule: {{I added it to CLAUDE.md.|It covers commit types and changelog headings.}}":
                "- Rule: I added it to CLAUDE.md.",
            "{{Three steps.|- a\n- b}}": "Three steps.",
        })

    def test_a_stretch_with_an_empty_long_form_is_plain_text(self) -> None:
        self.check({"- Current: {{every change gets an entry.|}} {{a| }} and {{a|a b}}":
                    "- Current: every change gets an entry. a and a"})

    def test_a_wordless_short_form_takes_on_the_words_of_its_sentence_before_it(self) -> None:
        self.check({
            "I changed a.ts. It merged 3 days ago{{.|, in #218, after review.}} Next.":
                "I changed a.ts. It merged 3 days ago. Next.",
            "- **Fix:** Done. {{|The hidden detail.}}": "- **Fix:** Done.",
            "Done. {{|- first\n- second}}": "Done.",
            "Tests: 3 pass{{.|; 1 skipped}}": "Tests: 3 pass.",
        })

    def test_a_wordless_short_form_with_no_words_to_take_on_is_drawn_open(self) -> None:
        self.check({
            "{{|Hidden.}} and - {{.|x}}": "Hidden. and - x",
            "Run `make`{{.|, twice.}}": "Run `make`, twice.",
            "It merged{{.|}} ok": "It merged. ok",
        })

    def test_a_lead_in_keeps_one_period_and_leaves_symbol_short_forms_alone(self) -> None:
        self.check({
            "Fixed the bug.{{.|x y}} Next": "Fixed the bug. Next",
            "See {{→|the details}}.": "See →.",
            "Fixed it{{.|, and also{{.|, more.}}}}": "Fixed it.",
        })

    def test_a_multi_line_short_form_is_drawn_open(self) -> None:
        self.check({"{{two\nlines|whole paragraph}}": "whole paragraph"})

    def test_a_short_form_shows_as_written(self) -> None:
        self.check({"See {{the [docs](x)|the long docs}}.": "See the [docs](x)."})

    def test_a_cut_off_stretch_is_drawn_open(self) -> None:
        self.check({
            "A {{b|b long}} c {{d|d half": "A b c d half",
            "A {{name}} {{}} {{short": "A {{name}} {{}} {{short",
            "A {{": "A {{",
            "Start {{short|long text cut off": "Start long text cut off",
            "It merged{{.|, in #218": "It merged, in #218",
            "Done{{.|\n- one": "Done\n\n- one",
            "see {{ and then a | table | row": "see  table | row",
        })

    def test_text_without_markup_is_returned_unchanged(self) -> None:
        self.check({"": "", "plain } text | here }}": "plain } text | here }}"})


if __name__ == "__main__":
    unittest.main()
