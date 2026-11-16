import json
from pathlib import Path

from riftdiff.cli import EXIT_DIFFERENT, EXIT_ERROR, EXIT_SAME, main

EXAMPLES = Path(__file__).resolve().parent.parent / "examples"
BEFORE = str(EXAMPLES / "customers_before.csv")
AFTER = str(EXAMPLES / "customers_after.csv")


def test_differences_exit_1_and_render(capsys):
    assert main([BEFORE, AFTER, "-k", "customer_id"]) == EXIT_DIFFERENT
    out = capsys.readouterr().out
    for text in ("Added rows", "Removed rows", "Changed rows", "Initech", "12000", "13500"):
        assert text in out


def test_identical_exit_0(capsys):
    assert main([BEFORE, BEFORE, "-k", "customer_id"]) == EXIT_SAME
    assert "No differences" in capsys.readouterr().out


def test_json_output(capsys):
    assert main([BEFORE, AFTER, "--key", "customer_id", "--json"]) == EXIT_DIFFERENT
    payload = json.loads(capsys.readouterr().out)
    assert payload["summary"]["rows_added"] == 2
    assert len(payload["changed"]) == 2


def test_json_summary_drops_rows(capsys):
    main([BEFORE, AFTER, "-k", "customer_id", "--json", "--summary"])
    payload = json.loads(capsys.readouterr().out)
    assert "added" not in payload and payload["summary"]["rows_changed"] == 2


def test_summary_hides_row_tables(capsys):
    main([BEFORE, AFTER, "-k", "customer_id", "--summary"])
    out = capsys.readouterr().out
    assert "Changed values by column" in out
    assert "Added rows" not in out


def test_limit_shows_more_caption(capsys):
    main([BEFORE, AFTER, "-k", "customer_id", "-n", "1"])
    assert "and 1 more" in capsys.readouterr().out


def test_ignore_and_comma_keys(capsys):
    code = main([BEFORE, AFTER, "-k", "customer_id,name", "-i", "revenue,status"])
    assert code == EXIT_DIFFERENT  # rows still added/removed
    assert "Changed rows" not in capsys.readouterr().out


def test_error_exit_2(capsys):
    assert main([BEFORE, AFTER, "-k", "nope"]) == EXIT_ERROR
    assert "not found" in capsys.readouterr().err


def test_negative_limit(capsys):
    assert main([BEFORE, AFTER, "-k", "customer_id", "-n", "-1"]) == EXIT_ERROR
