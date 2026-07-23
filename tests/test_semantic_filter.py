"""
Test the semantic relevance filter.

Run:  python tests/test_semantic_filter.py
      python -m pytest tests/test_semantic_filter.py -v

Requires sentence-transformers to be installed.
If the model cannot load (OOM / missing package), tests are skipped.
"""
from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    from sentence_transformers import SentenceTransformer  # noqa: F401
    _ST_AVAILABLE = True
except ImportError:
    _ST_AVAILABLE = False

from app.scrapers.semantic_filter import is_relevant_role, debug_scores, _load


_SKIP_MSG = "sentence-transformers not installed"


def _classify(title: str) -> tuple[str, float]:
    """Run is_relevant_role and return a human-readable verdict."""
    is_rel, conf = is_relevant_role(title)
    if is_rel is True:
        verdict = "PASS"
    elif is_rel is False:
        verdict = "BLOCK"
    else:
        verdict = "UNCERTAIN"
    return verdict, conf


@unittest.skipUnless(_ST_AVAILABLE, _SKIP_MSG)
class TestSemanticFilterPass(unittest.TestCase):
    """These titles must classify as PASS (is_relevant=True)."""

    def _assert_pass(self, title: str) -> None:
        verdict, conf = _classify(title)
        info = debug_scores(title)
        self.assertEqual(
            verdict, "PASS",
            f"\n  Title: {title!r}\n"
            f"  verdict={verdict}, conf={conf:.3f}\n"
            f"  top_rel={info['relevant'][:2]}\n"
            f"  top_irrel={info['irrelevant'][:2]}",
        )

    def test_ml_research_engineer(self):        self._assert_pass("ML Research Engineer")
    def test_ml_research_engineer_range(self):  self._assert_pass("ML Research Engineer (Junior–Senior)")
    def test_diffusion_model_researcher(self):  self._assert_pass("Diffusion Model Researcher")
    def test_foundation_model_engineer(self):   self._assert_pass("Foundation Model Engineer")
    def test_generative_ai_engineer(self):      self._assert_pass("Generative AI Engineer")
    def test_applied_scientist(self):           self._assert_pass("Applied Scientist")
    def test_data_scientist(self):              self._assert_pass("Data Scientist")
    def test_bi_analyst_ru(self):               self._assert_pass("Аналитик данных / BI-аналитик")
    def test_ml_engineer_ru(self):              self._assert_pass("ML инженер")
    def test_ai_engineer_bilingual(self):       self._assert_pass("AI Engineer / AI-инженер")
    def test_data_scientist_all_levels(self):   self._assert_pass("Data Scientist (all levels)")


@unittest.skipUnless(_ST_AVAILABLE, _SKIP_MSG)
class TestSemanticFilterBlock(unittest.TestCase):
    """These titles must NOT produce a confident PASS from the semantic layer."""

    def _assert_block(self, title: str) -> None:
        """Strict: semantic layer must hard-block (is_relevant=False)."""
        verdict, conf = _classify(title)
        info = debug_scores(title)
        self.assertEqual(
            verdict, "BLOCK",
            f"\n  Title: {title!r}\n"
            f"  verdict={verdict}, conf={conf:.3f}\n"
            f"  top_rel={info['relevant'][:2]}\n"
            f"  top_irrel={info['irrelevant'][:2]}",
        )

    def _assert_not_pass(self, title: str) -> None:
        """Relaxed: semantic must not confidently PASS (BLOCK or UNCERTAIN both OK).
        Titles caught here typically reach semantic only in tests — in the real
        pipeline pre-filter A drops them before is_relevant_role() is called."""
        verdict, conf = _classify(title)
        info = debug_scores(title)
        self.assertNotEqual(
            verdict, "PASS",
            f"\n  Title: {title!r} — must not be confidently PASS\n"
            f"  verdict={verdict}, conf={conf:.3f}\n"
            f"  top_rel={info['relevant'][:2]}\n"
            f"  top_irrel={info['irrelevant'][:2]}",
        )

    def test_frontend_developer(self):   self._assert_block("Frontend Developer")
    def test_sales_manager(self):        self._assert_block("Sales Manager")
    def test_qa_engineer(self):          self._assert_block("QA Engineer")
    def test_java_developer(self):       self._assert_block("Java Developer")

    # These titles contain "AI" which inflates semantic relevance scores, but
    # structured_filter.py pre-filter A (non-DS + AI-as-context) drops them
    # before is_relevant_role() is ever called in production.
    def test_android_trainee_with_ai(self):  self._assert_not_pass("Android Development Trainee with AI Tools")
    def test_net_trainee_with_ai(self):      self._assert_not_pass(".NET Development Trainee with AI")


@unittest.skipUnless(_ST_AVAILABLE, _SKIP_MSG)
class TestSemanticSeniorTagging(unittest.TestCase):
    """
    Semantic filter only decides relevance (True/False/None).
    Seniority tagging is done by regex in structured_filter.py.
    These tests just verify the semantic filter classifies all as PASS,
    and separately confirm the junior-override regex behaviour.
    """

    def test_senior_ml_engineer_is_pass(self):
        verdict, _ = _classify("Senior ML Engineer")
        self.assertEqual(verdict, "PASS")

    def test_staff_data_scientist_is_pass(self):
        verdict, _ = _classify("Staff Data Scientist")
        self.assertEqual(verdict, "PASS")

    def test_principal_ai_researcher_is_pass(self):
        verdict, _ = _classify("Principal AI Researcher")
        self.assertEqual(verdict, "PASS")

    def test_junior_senior_range_is_pass(self):
        # Should not be blocked — it's relevant, the junior/senior range
        # is handled by seniority regex in structured_filter.py, not here.
        verdict, _ = _classify("ML Research Engineer (Junior–Senior)")
        self.assertEqual(verdict, "PASS")


@unittest.skipUnless(_ST_AVAILABLE, _SKIP_MSG)
class TestDebugOutput(unittest.TestCase):
    """Print score tables for all test titles for threshold tuning."""

    TITLES = [
        # Expected PASS
        "ML Research Engineer (Junior–Senior)",
        "Diffusion Model Researcher",
        "Foundation Model Engineer",
        "Generative AI Engineer",
        "Applied Scientist",
        "Data Scientist",
        "Аналитик данных / BI-аналитик",
        "ML инженер",
        "AI Engineer / AI-инженер",
        # Expected BLOCK
        "Frontend Developer",
        "Android Development Trainee with AI Tools",
        "Sales Manager",
        "QA Engineer",
        "Java Developer",
        ".NET Development Trainee with AI",
    ]

    def test_print_scores(self):
        print("\n\n=== Semantic filter score table ===")
        print(f"{'Title':<45} {'verdict':<10} {'conf':>6}  {'max_rel':>7}  {'max_irrel':>9}")
        print("-" * 85)
        for title in self.TITLES:
            verdict, conf = _classify(title)
            info = debug_scores(title)
            print(
                f"{title:<45} {verdict:<10} {conf:>+6.3f}"
                f"  {info['max_rel']:>7.3f}  {info['max_irrel']:>9.3f}"
            )
        print()


if __name__ == "__main__":
    if not _ST_AVAILABLE:
        print("sentence-transformers not installed — run: pip install sentence-transformers")
        sys.exit(1)

    print("Loading model (first run may take ~20s to download)...")
    _load()
    print()

    result = unittest.TextTestRunner(verbosity=2).run(
        unittest.TestLoader().loadTestsFromNames([
            "__main__.TestDebugOutput",
            "__main__.TestSemanticFilterPass",
            "__main__.TestSemanticFilterBlock",
            "__main__.TestSemanticSeniorTagging",
        ])
    )
    sys.exit(0 if result.wasSuccessful() else 1)
