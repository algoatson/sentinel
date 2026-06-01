"""is_routine_payout_headline — drops income-fund/ETF payout boilerplate
without eating material company news. Patterns taken from the live feed."""

from sentinel.utils import is_routine_payout_headline as routine


def test_filters_etf_distribution_notices():
    assert routine("The Industrial Select Sector SPDR Premium Income Fund declares monthly distribution of $0.36")
    assert routine("iShares 3-7 Year Treasury Bond ETF declares monthly distribution of $0.369")
    assert routine("State Street Short Duration IG Public & Private Credit ETF declares monthly distribution")
    assert routine("SPDR SSGA My2026 Municipal Bond ETF declares monthly distribution of $0.04")


def test_filters_closed_end_fund_dividend_declarations():
    assert routine("PGIM High Yield Bond Fund declares $0.105 dividend")
    assert routine("PGIM Short Duration High Yield Opportunities Fund declares $0.108 dividend")


def test_keeps_material_company_news():
    # company dividend — no fund/ETF marker → keep
    assert not routine("Apple declares quarterly dividend of $0.25 per share")
    # editorial dividend article — no declaration verb → keep
    assert not routine("2 ETFs Paying Reliable Dividends in an Uncertain Market")
    # a real deal that merely contains the word "distribution" → keep
    assert not routine("Pfizer announces distribution agreement with McKesson")
    # ordinary headlines
    assert not routine("Merck exec says company is discussing COVID antiviral deal")
    assert not routine("Greg Abel just made his first big deal as Berkshire CEO")
    assert not routine("")
    assert not routine(None)
