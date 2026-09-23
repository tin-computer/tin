"""growth.change_check judges each dated change with a weekly process behaviour chart."""

import json
import runpy
from datetime import date, timedelta
from types import SimpleNamespace

from tin_lite.community import REPOSITORY_ROOT
from tin_lite.workflow_code import validate_code_definition, validate_code_result

PACKAGE = REPOSITORY_ROOT / "workflow_packages" / "growth.change_check"
DEFINITION = json.loads((PACKAGE / "workflow.json").read_text())["definition"]
SPEC = validate_code_definition(DEFINITION)
# Tests execute this reviewed package directly; static package validation never imports it.
MODULE = SimpleNamespace(**runpy.run_path(str(PACKAGE / "main.py")))

CHANGE = date(2026, 6, 1)
# Twelve flat weeks with real week-to-week variation: mean 1207/12, average moving range 50/11,
# so the natural process limits are 88.5 and 112.7.
FLAT = [100, 104, 98, 102, 97, 103, 101, 99, 105, 96, 100, 102]
# Daily offsets that cancel within each week, so every weekly average is exact.
WEEKDAY = [6, 3, 0, 0, -3, -3, -3]
NOISE = [1.5, -2.0, 0.5, 2.5, -1.0, -2.5, 1.0, 0.0, -1.5, 2.0, -0.5, 1.5, 0.5, -1.5, 1.0, -0.5]
GROWTH = [1.01, 0.985, 1.005, 1.02, 0.99, 0.98, 1.01, 1.0, 0.985, 1.015, 0.995, 1.01]


def weekly_csv(weeks, start, offsets=WEEKDAY, digits=4):
    rows = ["date,value"]
    for index, level in enumerate(weeks):
        for day, offset in enumerate(offsets):
            value = level + offset if digits is None else round(level + offset, digits)
            rows.append(f"{start + timedelta(days=7 * index + day)},{value}")
    return "\n".join(rows) + "\n"


def judge(weeks=None, changes=("2026-06-01 | New pricing page",), *, start=None, ctx=None, **extra):
    if weeks is not None:
        extra.setdefault("series_csv", weekly_csv(weeks, start or CHANGE - timedelta(days=84)))
    # The dashboard sends the change log as one textarea: one change per line.
    lines = changes if isinstance(changes, str) else "\n".join(changes)
    inputs = {"metric_name": "Daily signups", "changes": lines, **extra}
    result = MODULE.run(ctx, inputs)
    validate_code_result(json.dumps(result).encode(), SPEC)
    return result["content"]


def row(day, verdict):
    return f"| {day} | {verdict} |"


def summary(content, day):
    return next(line for line in content.splitlines() if line.startswith(f"| {day} |"))


def section(content, day):
    return content.split(f"## {day}:", 1)[1].split("\n## ", 1)[0]


def test_the_package_is_bounded_code_with_no_model_calls():
    assert DEFINITION["executor"] == "workflow.code"
    assert "model_routes" not in DEFINITION["code"]


def test_a_lasting_step_up_is_a_move_with_exact_limits():
    content = judge(FLAT + [118, 121, 116, 119])
    assert row("2026-06-01", "Moved up") in content
    assert "100.6" in content and "88.5 to 112.7" in content


def test_a_lasting_move_is_reported_before_four_weeks():
    assert row("2026-06-01", "Moved up") in judge(FLAT + [118, 121])


def test_moves_up_and_down_get_different_next_steps():
    up = summary(judge(FLAT + [118, 121, 116, 119]), "2026-06-01")
    down = summary(judge(FLAT + [80, 78, 83, 81]), "2026-06-01")
    assert up.startswith(row("2026-06-01", "Moved up")) and "Keep" in up
    assert down.startswith(row("2026-06-01", "Moved down")) and "Investigate or revert" in down


def test_routine_variation_is_no_detectable_change_with_the_smallest_catchable_move():
    content = judge(FLAT + [101, 99, 103, 100])
    assert row("2026-06-01", "No detectable change") in content
    assert "12.1 per day" in content


def test_two_weeks_in_is_too_early_with_a_recheck_date():
    content = judge(FLAT + [101, 99])
    assert row("2026-06-01", "Too early") in content
    assert "2026-06-30" in content


def test_the_recheck_date_leaves_room_for_a_complete_fourth_week():
    weeks = FLAT + [101, 99, 103, 100]
    early = judge(weeks, ctx={"created_at": "2026-06-29T12:00:00Z"})
    assert row("2026-06-01", "Too early") in early and "2026-06-30" in early
    on_time = judge(weeks, ctx={"created_at": "2026-06-30T00:00:00Z"})
    assert row("2026-06-01", "No detectable change") in on_time


def test_a_single_spike_is_a_one_off_not_a_move():
    assert row("2026-06-01", "One-off spike") in judge(FLAT + [150, 101, 99, 102])


def test_weeks_on_both_sides_are_not_a_lasting_move():
    content = judge(FLAT + [150, 50, 101, 99])
    assert row("2026-06-01", "No detectable change") in content
    assert "both above and below" in content and "stayed within" not in content


def test_a_linear_trend_that_simply_continues_is_not_credited_to_the_change():
    content = judge([100 + 3 * week + NOISE[week] for week in range(16)])
    assert row("2026-06-01", "No detectable change") in content


def test_compounding_growth_that_simply_continues_is_not_credited_to_the_change():
    noise = GROWTH + [1.005, 0.99, 1.01, 0.995]
    content = judge([round(100 * 1.05**week * noise[week], 2) for week in range(16)])
    assert row("2026-06-01", "No detectable change") in content


def test_growth_that_stalls_after_the_change_moved_down_against_the_trend():
    before = [round(100 * 1.05**week * GROWTH[week], 2) for week in range(12)]
    content = judge(before + [172, 174, 171, 173])
    assert row("2026-06-01", "Moved down") in content


def test_a_trend_through_zero_is_charted_on_the_raw_values():
    weeks = [3 * week + (NOISE[week] if week else 0) for week in range(16)]
    content = judge(series_csv=weekly_csv(weeks, CHANGE - timedelta(days=84), offsets=[0] * 7))
    assert row("2026-06-01", "No detectable change") in content
    assert "per day each week" in content


def test_a_metric_that_can_be_negative_keeps_its_lower_limit():
    content = judge([level - 150 for level in FLAT] + [-32, -29, -34, -31])
    assert row("2026-06-01", "Moved up") in content and "-61.5 to -37.3" in content


def test_a_low_count_metric_has_no_lower_limit_below_zero():
    weeks = [3, 1, 4, 2, 5, 1, 3, 2, 4, 1, 3, 2, 2, 3, 1, 2]
    content = judge(series_csv=weekly_csv(weeks, CHANGE - timedelta(days=84), offsets=[0] * 7))
    assert row("2026-06-01", "No detectable change") in content and "up to 8.6" in content


def test_rates_keep_their_precision():
    weeks = [level / 1000 for level in FLAT] + [0.118, 0.121, 0.116, 0.119]
    offsets = [offset / 1000 for offset in WEEKDAY]
    series = weekly_csv(weeks, CHANGE - timedelta(days=84), offsets=offsets, digits=6)
    content = judge(series_csv=series)
    assert row("2026-06-01", "Moved up") in content and "0.088 to 0.113" in content


def test_small_variation_on_larger_values_stays_readable():
    weeks = [2.30 + 0.008 * (level - 100) for level in FLAT] + [2.40, 2.41, 2.39, 2.40]
    offsets = [offset / 1000 for offset in WEEKDAY]
    series = weekly_csv(weeks, CHANGE - timedelta(days=84), offsets=offsets, digits=6)
    content = judge(series_csv=series, metric_name="Conversion rate, %")
    assert row("2026-06-01", "Moved up") in content and "top quarter" in content
    assert "| 2026-06-01 | 2.400 | 2.208 to 2.401 | after, inside |" in content
    assert "| 2026-06-08 | 2.410 | 2.208 to 2.401 | after, above |" in content


def test_changes_too_close_together_are_not_separated_either_way():
    content = judge(
        FLAT + [101, 99, 103, 100, 98, 102],
        ["2026-06-01 | New pricing page", "2026-06-11 | New onboarding email"],
    )
    assert row("2026-06-01", "Can't separate") in content
    assert row("2026-06-11", "Can't separate") in content


def test_an_unjudged_earlier_change_is_never_credited_to_the_next_one():
    content = judge(
        FLAT + [118, 121, 116, 119, 120, 117, 122], ["2026-06-01 | a", "2026-06-02 | b"]
    )
    assert row("2026-06-01", "Can't separate") in content
    assert row("2026-06-02", "Can't separate") in content and "| Moved" not in content


def test_an_earlier_step_does_not_read_as_a_later_drop():
    weeks = FLAT[:9] + [level + 18 for level in FLAT + FLAT[:1]]
    content = judge(weeks, ["2026-03-09 | a", "2026-04-27 | b"], start=date(2026, 1, 5))
    assert row("2026-03-09", "Can't judge") in content
    assert row("2026-04-27", "Can't judge") in content and "| Moved" not in content


def test_nine_weeks_of_history_is_not_enough_but_ten_is():
    short = judge(FLAT[:9] + [118, 121, 116, 119], start=CHANGE - timedelta(days=63))
    assert row("2026-06-01", "Can't judge") in short and "9 of 10" in short
    enough = judge(FLAT[:10] + [118, 121, 116, 119], start=CHANGE - timedelta(days=70))
    assert row("2026-06-01", "Moved up") in enough


def test_an_earlier_change_that_moved_the_metric_starts_the_next_baseline():
    content = judge(
        FLAT + [118, 121, 116, 119, 120, 117, 122, 118, 119, 121],
        ["2026-06-01 | New pricing page", "2026-07-13 | New onboarding email"],
    )
    assert row("2026-06-01", "Moved up") in content
    assert row("2026-07-13", "Can't judge") in content and "6 of 10" in content


def test_an_unexplained_spike_before_the_change_makes_the_baseline_unstable_until_it_is_logged():
    before = FLAT + FLAT[:11]
    before[12] = 160
    weeks = before + [101, 99, 103, 100]
    start = CHANGE - timedelta(days=161)
    unlogged = judge(weeks, ["2026-06-01 | New pricing page"], start=start)
    assert row("2026-06-01", "Can't judge") in unlogged and "2026-03-16" in unlogged
    logged = judge(
        weeks, ["2026-03-16 | Newsletter mention", "2026-06-01 | New pricing page"], start=start
    )
    assert row("2026-03-16", "One-off spike") in logged
    assert row("2026-06-01", "No detectable change") in logged


def test_logging_an_event_always_changes_what_a_later_baseline_counts():
    before = FLAT + FLAT[:1]
    before[7] = 160
    weeks, start = before + [101, 99, 103, 100], CHANGE - timedelta(days=91)
    unlogged = judge(weeks, ["2026-06-01 | New pricing page"], start=start)
    assert "unsteady" in section(unlogged, "2026-06-01")
    logged = judge(weeks, ["2026-04-20 | Big mention", "2026-06-01 | Pricing"], start=start)
    later = section(logged, "2026-06-01")
    assert "unsteady" not in later and "counting from 2026-05-18" in later


def test_three_of_four_weeks_near_a_limit_make_the_baseline_unsteady():
    weeks = [100, 101, 99, 100, 101, 106, 106, 106, 100, 99, 101, 100, 101, 100, 99, 101]
    content = judge(weeks)
    assert row("2026-06-01", "Can't judge") in content
    # No week is beyond a limit; three of four in the upper quarter from 2026-04-06 is rule 3.
    assert "unsteady (week of 2026-04-06)" in content


def test_a_running_total_is_refused_as_unusable_input():
    total, rows = 0, ["date,value"]
    for day in range(60):
        total += 10 + day % 5
        rows.append(f"{date(2026, 1, 1) + timedelta(days=day)},{total}")
    content = judge(series_csv="\n".join(rows), changes=["2026-02-01 | Launch"])
    assert "Nothing was judged" in content and "running total" in content
    assert "daily change" in content


def test_a_metric_with_no_variation_cannot_be_judged():
    content = judge(series_csv=weekly_csv([0] * 16, CHANGE - timedelta(days=84), offsets=[0] * 7))
    assert row("2026-06-01", "Can't judge") in content
    assert "no week-to-week variation" in content


def test_a_noise_free_trend_is_not_mistaken_for_a_move():
    weeks, start = [100 * 1.05**week for week in range(16)], CHANGE - timedelta(days=84)
    exact = judge(series_csv=weekly_csv(weeks, start, offsets=[0] * 7, digits=None))
    assert row("2026-06-01", "Can't judge") in exact
    assert "no week-to-week variation" in exact
    # Written to four decimals, the only noise left is rounding; it must still not read as a move.
    rounded = judge(series_csv=weekly_csv(weeks, start, offsets=[0] * 7))
    assert "| Moved" not in rounded and "under 1%" in rounded


def test_a_likely_partial_last_day_is_left_out_and_said_so():
    series = weekly_csv(FLAT + [101, 99, 103, 100], CHANGE - timedelta(days=84))
    series = series.replace("2026-06-28,97", "2026-06-28,20")
    content = judge(series_csv=series, ctx={"created_at": "2026-06-28T09:30:00Z"})
    assert "Left out 2026-06-28" in content
    assert row("2026-06-01", "Too early") in content


def test_missing_days_are_refused_unless_they_count_as_zero():
    series = weekly_csv(FLAT + [101, 99, 103, 100], CHANGE - timedelta(days=84))
    for day in ("2026-06-09", "2026-06-10", "2026-06-11"):
        series = "\n".join(line for line in series.split("\n") if not line.startswith(day))
    refused = judge(series_csv=series)
    assert "Nothing was judged" in refused and "2026-06-09 to 2026-06-11" in refused
    counted = judge(series_csv=series, missing_days_are_zero=True)
    assert row("2026-06-01", "One-off dip") in counted


def test_every_input_problem_is_listed_in_one_diagnostic():
    series = "\n".join(
        [
            "date,value",
            "2026-01-01,10",
            "2026-01-01,11",
            "2026-01-02,ten",
            "2026-01-03,12,13",
            "2026-01-05,13",
            "2026-02-30,14",
        ]
    )
    content = judge(
        series_csv=series,
        changes=["2026-01-02 New pricing page", "2026-01-03 | Launch", "2026-01-03 | Other"],
    )
    assert "Nothing was judged" in content
    for problem in ["2026-01-01", "ten", "two columns", "2026-01-04", "2026-02-30"]:
        assert problem in content
    assert "what changed" in content and "combine" in content


def test_a_header_with_the_wrong_number_of_columns_is_refused():
    content = judge(series_csv="date,value,note\n2026-01-01,1,x\n")
    assert "Nothing was judged" in content and "exactly two columns" in content
    tabs = judge(series_csv="date\tvalue\n2026-01-01\t5\n")
    assert "separated by a comma" in tabs and "Paste at least" not in tabs


def test_a_date_span_beyond_the_limit_is_refused_without_listing_every_day():
    content = judge(series_csv="date,value\n2020-01-01,1\n2026-01-01,2\n")
    assert "Nothing was judged" in content and "1,000" in content
    assert len(content.encode()) < 2000


def test_inputs_that_used_to_crash_get_a_report_or_a_diagnostic():
    carriage = weekly_csv(FLAT + [118, 121, 116, 119], CHANGE - timedelta(days=84))
    assert row("2026-06-01", "Moved up") in judge(series_csv=carriage.replace("\n", "\r"))
    assert "Nothing was judged" in judge(series_csv="date,value\n2026-01-01,1\r5\n")
    extremes = judge(FLAT + [101, 99, 103, 100], ["9999-12-31 | x", "0001-01-01 | y"])
    assert "Nothing was judged" in extremes and "9999-12-31" in extremes
    huge = judge(series_csv="date,value\n2026-01-01,1e308\n2026-01-02,inf\n2026-01-03,nan\n")
    assert "Nothing was judged" in huge and "1e308" in huge and "nan" in huge


def test_a_byte_order_mark_does_not_hide_the_first_row():
    series = weekly_csv(FLAT[:10] + [118, 121, 116, 119], CHANGE - timedelta(days=70))
    content = judge(series_csv="\ufeff" + series.split("\n", 1)[1])
    assert row("2026-06-01", "Moved up") in content


def test_user_text_cannot_break_the_report_tables():
    content = judge(
        FLAT + [118, 121, 116, 119],
        ["2026-06-01 | Pricing | v2 launch 20\\|30"],
        metric_name="Sign|ups\nweekly",
    )
    assert row("2026-06-01", "Moved up") in content
    assert "Pricing \\| v2 launch 20\\\\\\|30" in content and "Sign\\|ups weekly" in content


def test_changes_are_one_per_line_and_commas_are_fine():
    pasted = "\r\n2026-06-01 | New pricing, and a banner\r\n\r\n"
    content = judge(FLAT + [118, 121, 116, 119], pasted)
    assert row("2026-06-01", "Moved up") in content and "New pricing, and a banner" in content


def test_too_many_or_too_long_change_lines_are_refused():
    many = judge(FLAT + [101, 99, 103, 100], [f"2026-06-{day:02d} | c" for day in range(1, 12)])
    assert "Nothing was judged" in many and "at most 10" in many
    long = judge(FLAT + [101, 99, 103, 100], ["2026-06-01 | " + "x" * 228])
    assert "Nothing was judged" in long and "240 characters" in long


def test_weeks_that_swing_both_ways_do_not_widen_a_later_baseline():
    weeks = FLAT + [150, 50, 101, 99] + FLAT[:6] + [118, 121, 116, 119]
    content = judge(weeks, ["2026-06-01 | Redesign", "2026-08-10 | New pricing page"])
    assert row("2026-06-01", "No detectable change") in content
    later = section(content, "2026-08-10")
    assert "Can't judge" in later and "6 of 10" in later and "swung both ways" in later


def test_changes_outside_the_data_get_their_own_verdicts():
    content = judge(
        FLAT + [101, 99, 103, 100],
        [
            "2026-02-01 | Before the data",
            "2026-06-01 | Real change",
            "2026-07-20 | Future launch",
            "2026-07-25 | Follow-up",
        ],
    )
    assert row("2026-02-01", "Can't judge") in content and "0 of 10" in content
    assert row("2026-06-01", "No detectable change") in content
    assert row("2026-07-20", "Can't separate") in content
    assert row("2026-07-25", "Can't separate") in content


def test_a_change_just_before_the_data_still_blocks_its_four_weeks():
    content = judge(
        FLAT + [101, 99, 103, 100], ["2026-03-01 | Before the data", "2026-06-01 | Real change"]
    )
    assert row("2026-03-01", "Can't judge") in content and "0 of 10" in content
    later = section(content, "2026-06-01")
    assert "Can't judge" in later and "9 of 10" in later and "counting from 2026-03-29" in later


def test_a_change_after_the_data_is_too_early_with_a_recheck_date():
    content = judge(FLAT + [101, 99, 103, 100], ["2026-07-20 | Future launch"])
    assert row("2026-07-20", "Too early") in content and "2026-08-18" in content


def test_the_largest_report_and_diagnostic_fit_the_declared_output():
    start, limit = date(2024, 1, 1), DEFINITION["code"]["output"]["max_bytes"]
    # Negative twelve-digit values print as wide as a charted number gets: a variation small
    # enough to need more decimals is below the no-variation tolerance at this size.
    levels = [-999_999_000_000 + 1000 * (FLAT[week % 12] - 100) for week in range(52)]
    series = weekly_csv(levels, start, offsets=[1000 * offset for offset in WEEKDAY])
    assert len(series) <= DEFINITION["input_schema"]["properties"]["series_csv"]["maxLength"]
    # A change line is at most 240 characters, and pipes and backslashes each escape to two.
    changes = [f"{start + timedelta(days=84 + 28 * index)} | {'\\|' * 113}x" for index in range(10)]
    assert all(len(change) == 240 for change in changes)
    field = DEFINITION["input_schema"]["properties"]["changes"]
    assert len("\n".join(changes)) <= field["maxLength"]
    report = judge(series_csv=series, changes=changes, metric_name="\\|" * 40)
    assert report.count("| No detectable change |") == 10 and "-999,998,999,416.7" in report
    assert len(report.encode()) <= limit
    diagnostic = judge(series_csv="x,y\n" * 3999, changes=["z" * 240] * 10)
    assert "Nothing was judged" in diagnostic and len(diagnostic.encode()) <= limit


def test_the_same_input_always_gives_the_same_report():
    weeks = FLAT + [118, 121, 116, 119]
    assert judge(weeks) == judge(weeks)
