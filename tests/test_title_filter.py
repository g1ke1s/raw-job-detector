"""
Unit tests for the title filter pre-filter logic.

Runs locally without SQLAlchemy or a real database by pre-injecting lightweight
fakes for app.db.config_store, app.monitoring.events, and app.llm.client into
sys.modules before any app code is imported.

Run:  python tests/test_title_filter.py
      python -m pytest tests/test_title_filter.py -v
"""
from __future__ import annotations

import asyncio
import sys
import os
import unittest
from unittest.mock import MagicMock, AsyncMock

# ── Pre-inject stub modules BEFORE importing any app code ─────────────────────
# Prevents the SQLAlchemy / DB import chain from being triggered.
# Only the leaf modules that title_seniority_check() lazy-imports are stubbed.
_fake_events = MagicMock()
_fake_events.log_event = AsyncMock()
sys.modules["app.monitoring.events"] = _fake_events

_fake_config = MagicMock()          # .get will be overwritten per test run
sys.modules["app.db.config_store"] = _fake_config

_fake_llm = MagicMock()
sys.modules["app.llm.client"] = _fake_llm
# ──────────────────────────────────────────────────────────────────────────────

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.scrapers.structured_filter import (
    _norm,
    _RE_NON_DS,
    _RE_AI_AS_CONTEXT,
    _RE_COURSE_SIGNAL,
    _CONTEXT_ABBREV,
    title_seniority_check,
)
import app.scrapers.structured_filter as _filter_mod

# ── Default keyword lists (mirrors config_store._DEFAULTS) ────────────────────
_INCLUDE_STRONG = [
    "data scientist", "ml engineer", "machine learning engineer",
    "ai engineer", "mlops", "data engineer", "data analyst",
    "nlp engineer", "computer vision engineer", "ml researcher",
    "deep learning engineer", "data science", "analytics engineer",
    "bi analyst", "bi developer", "bi engineer",
    "llm engineer", "llm developer", "machine learning",
    "artificial intelligence engineer", "applied scientist",
    "research scientist", "decision scientist",
    "ml", "ai", "nlp", "cv engineer", "mle",
    "ds", "de", "da",
]
_EXCLUDE_HARD = [
    "java developer", "backend developer", "frontend developer",
    "backend engineer", "frontend engineer", "backend", "frontend",
    "fullstack", "full-stack", "system analyst", "business analyst",
    "qa engineer", "tester", "sdet", "devops engineer",
    "android developer", "ios developer", "php developer",
    "golang developer", "1c developer", "sales manager",
    "hr manager", "hr generalist", "recruiter", "designer", "ui/ux",
    "project manager", "accountant",
]
_SENIOR_TERMS = [
    "senior", "lead", "head", "principal", "staff", "director",
    "chief", "вед.", "ведущий", "руководитель", "главный", "старший",
    "team lead", "tech lead",
]


def _make_db_get(strong=None, hard=None, senior=None):
    mapping = {
        "include_strong": strong if strong is not None else _INCLUDE_STRONG,
        "exclude_hard":   hard   if hard   is not None else _EXCLUDE_HARD,
        "senior_terms":   senior if senior is not None else _SENIOR_TERMS,
    }
    async def _get(key):
        return mapping.get(key, [])
    return _get


def run_check(title: str, db_get_fn=None) -> str:
    """Synchronous helper: run title_seniority_check with mocked DB and LLM."""
    if db_get_fn is None:
        db_get_fn = _make_db_get()

    async def _run():
        # Point the already-injected config stub at the test's db_get function
        sys.modules["app.db.config_store"].get = db_get_fn

        # LLM always returns False — pre-filters should fire before it anyway
        _filter_mod._llm_title_cache.clear()
        orig_llm = _filter_mod._llm_title_check
        _filter_mod._llm_title_check = AsyncMock(return_value=False)
        try:
            return await title_seniority_check(title)
        finally:
            _filter_mod._llm_title_check = orig_llm

    return asyncio.run(_run())


# ── Pattern-level tests (sync, no mocking needed) ─────────────────────────────

class TestNorm(unittest.TestCase):
    def test_hyphen_to_space(self): self.assertEqual(_norm("Front-End"), "front end")
    def test_dot_stripped(self):    self.assertEqual(_norm(".NET"), "net")
    def test_slash_to_space(self):  self.assertEqual(_norm("AI/ML"), "ai ml")
    def test_lowercase(self):       self.assertEqual(_norm("Android Development"), "android development")


class TestNonDsPattern(unittest.TestCase):
    def _m(self, s): return bool(_RE_NON_DS.search(_norm(s)))

    def test_frontend(self):      self.assertTrue(self._m("Front-End Development"))
    def test_android(self):       self.assertTrue(self._m("Android Development"))
    def test_net(self):           self.assertTrue(self._m(".NET Development"))
    def test_java(self):          self.assertTrue(self._m("Java Development"))
    def test_func_testing(self):  self.assertTrue(self._m("Software Functional Testing"))
    def test_backend(self):       self.assertTrue(self._m("Backend Engineer"))
    def test_devops(self):        self.assertTrue(self._m("DevOps Engineer"))

    def test_no_data_scientist(self): self.assertFalse(self._m("Data Scientist"))
    def test_no_ml_engineer(self):    self.assertFalse(self._m("ML Engineer"))
    def test_no_mlops(self):          self.assertFalse(self._m("MLOps Engineer"))   # "ios" inside "mlops" must NOT match
    def test_no_network(self):        self.assertFalse(self._m("Network Engineer"))  # "net" inside "network" must NOT match


class TestAiContextPattern(unittest.TestCase):
    def _m(self, s): return bool(_RE_AI_AS_CONTEXT.search(_norm(s)))

    def test_with_ai(self):           self.assertTrue(self._m("Frontend Dev with AI"))
    def test_ai_tools(self):          self.assertTrue(self._m("Android Dev with AI Tools"))
    def test_generative_ai_for(self): self.assertTrue(self._m("Generative AI for Software Dev"))
    def test_using_ai(self):          self.assertTrue(self._m("Java Dev using AI"))
    def test_ai_powered(self):        self.assertTrue(self._m("AI-Powered DevOps"))

    def test_no_ai_engineer(self):            self.assertFalse(self._m("AI Engineer"))
    def test_no_ml_engineer(self):            self.assertFalse(self._m("ML Engineer"))
    def test_no_generative_ai_engineer(self): self.assertFalse(self._m("Generative AI Engineer"))


class TestCourseSignal(unittest.TestCase):
    def _m(self, s): return bool(_RE_COURSE_SIGNAL.search(_norm(s)))

    def test_android_trainee(self):   self.assertTrue(self._m("Android Development Trainee"))
    def test_func_test_trainee(self): self.assertTrue(self._m("Functional Testing Trainee"))
    def test_net_trainee(self):       self.assertTrue(self._m(".NET Development Trainee with AI"))


# ── Integration tests (async pipeline, mocked DB + LLM) ──────────────────────

class TestFilterFail(unittest.TestCase):
    """Must be EXCLUDED — return 'drop'."""

    def test_frontend_trainee_with_ai(self):
        # Pre-filter A: "front end" (non-DS) + "with ai" (context qualifier)
        self.assertEqual(run_check("Front-End Development Trainee with AI"), "drop")

    def test_android_trainee_with_ai(self):
        # Pre-filter A: android + "with ai"; also Pre-filter B: android + trainee
        self.assertEqual(run_check("Android Development Trainee with AI"), "drop")

    def test_net_trainee_with_ai(self):
        # Pre-filter A: "net" (.NET normalised) + "with ai"
        self.assertEqual(run_check(".NET Development Trainee with AI"), "drop")

    def test_functional_testing_trainee(self):
        # Pre-filter B: "functional testing" (non-DS) + "trainee"; no AI in title
        self.assertEqual(run_check("Software Functional Testing Trainee"), "drop")

    def test_java_with_ai_tools(self):
        # Pre-filter A: "java" (non-DS) + "ai tools" (context qualifier)
        self.assertEqual(run_check("Java Development with AI Tools"), "drop")

    def test_generative_ai_for_software_dev(self):
        # _RE_AI_AS_CONTEXT fires on "generative ai for" → bare "ai" suppressed →
        # has_strong=False → no has_hard → LLM mock returns False → drop
        self.assertEqual(run_check("Generative AI for Software Development"), "drop")


class TestFilterPass(unittest.TestCase):
    """Must PASS — return 'pass' or 'senior', never 'drop'."""

    def test_ml_engineer(self):
        # "ml engineer" in strong_phrase
        self.assertNotEqual(run_check("ML Engineer"), "drop")

    def test_ai_engineer(self):
        # "ai engineer" in strong_phrase
        self.assertNotEqual(run_check("AI Engineer"), "drop")

    def test_data_scientist_trainee(self):
        # "data scientist" in strong_phrase → has_strong_phrase=True → pre-filter B skipped
        self.assertNotEqual(run_check("Data Scientist Trainee"), "drop")

    def test_junior_data_analyst(self):
        # "data analyst" in strong_phrase
        self.assertNotEqual(run_check("Junior Data Analyst"), "drop")

    def test_mlops_intern(self):
        # "mlops" in strong_phrase
        self.assertNotEqual(run_check("MLOps Intern"), "drop")

    def test_ai_ml_engineer(self):
        # "AI/ML Engineer" → norm "ai ml engineer" → "ml engineer" in strong_phrase
        self.assertNotEqual(run_check("AI/ML Engineer"), "drop")

    def test_generative_ai_engineer(self):
        # "ai engineer" in strong_phrase
        self.assertNotEqual(run_check("Generative AI Engineer"), "drop")

    def test_devops_mlops_engineer(self):
        # "DevOps/MLOps" — "devops" is non-DS but "mlops" in strong_phrase →
        # has_strong_phrase=True → pre-filters guarded → not dropped
        self.assertNotEqual(run_check("DevOps/MLOps Engineer"), "drop")

    def test_ml_backend_engineer(self):
        # "backend" is non-DS but no AI qualifier → ai_is_context=False →
        # "ml" bare abbrev not blocked → has_strong_abbrev=True → passes
        self.assertNotEqual(run_check("ML Backend Engineer"), "drop")


if __name__ == "__main__":
    print("Running title filter tests...\n")
    suite = unittest.TestLoader().loadTestsFromNames([
        "__main__.TestNorm",
        "__main__.TestNonDsPattern",
        "__main__.TestAiContextPattern",
        "__main__.TestCourseSignal",
        "__main__.TestFilterFail",
        "__main__.TestFilterPass",
    ])
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    sys.exit(0 if result.wasSuccessful() else 1)
