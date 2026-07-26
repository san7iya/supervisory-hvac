"""Pure date-chunking logic and RunPeriod field edits. No I/O beyond the IDF
object passed in, no reasoning -- this is mechanical plumbing only."""
from datetime import date, timedelta

# Non-leap reference year used only for month/day arithmetic; RunPeriod's
# actual Year fields are left blank in the IDF so EnergyPlus infers the year
# from the weather file, same as the bundled example already does.
_REF_YEAR = 2001


def chunk_dates(start_month, start_day, end_month, end_day, chunk_days=7):
    """Split [start, end] (inclusive) into consecutive chunk_days-long windows.

    Returns a list of (begin_month, begin_day, end_month, end_day) tuples.
    The final chunk is shortened rather than overrunning the end date.
    """
    start = date(_REF_YEAR, start_month, start_day)
    end = date(_REF_YEAR, end_month, end_day)
    if end < start:
        raise ValueError("end date is before start date")

    chunks = []
    cur = start
    while cur <= end:
        chunk_end = min(cur + timedelta(days=chunk_days - 1), end)
        chunks.append((cur.month, cur.day, chunk_end.month, chunk_end.day))
        cur = chunk_end + timedelta(days=1)
    return chunks


def set_run_period(idf, run_period_name, begin_month, begin_day, end_month, end_day):
    """Overwrite the date fields of the named RunPeriod object in place."""
    rp = idf.getobject("RunPeriod", run_period_name)
    if rp is None:
        raise ValueError(f"RunPeriod {run_period_name!r} not found")
    rp.Begin_Month = begin_month
    rp.Begin_Day_of_Month = begin_day
    rp.End_Month = end_month
    rp.End_Day_of_Month = end_day