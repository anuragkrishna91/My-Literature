"""Technical analysis: RSI and rule-based recommendation signals.

The "recommendations" here are transparent, mechanical signals computed
from price history (trend, momentum, RSI). Every point of the score comes
with a human-readable reason. They are informational only — not financial
advice.
"""


def rsi(closes, period=14):
    """Relative Strength Index (Wilder's smoothing). None if history is
    too short. 0-100; classically <30 is 'oversold', >70 'overbought'."""
    if len(closes) < period + 1:
        return None
    deltas = [closes[i + 1] - closes[i] for i in range(len(closes) - 1)]
    seed = deltas[:period]
    avg_gain = sum(d for d in seed if d > 0) / period
    avg_loss = sum(-d for d in seed if d < 0) / period
    for d in deltas[period:]:
        avg_gain = (avg_gain * (period - 1) + max(d, 0.0)) / period
        avg_loss = (avg_loss * (period - 1) + max(-d, 0.0)) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - 100.0 / (1.0 + rs)


LABELS = [
    (4, "Strong"),      # score >= 4
    (2, "Positive"),    # 2..3
    (-1, "Neutral"),    # -1..1
]
WEAK_LABEL = "Weak"     # <= -2


def recommend(quote):
    """Score a Quote and explain why.

    Returns {"score": int, "label": str, "rsi": float|None,
             "reasons": [str, ...]}.
    """
    score = 0
    reasons = []
    r = rsi(quote.closes)

    if quote.trend == "up":
        score += 2
        reasons.append("uptrend: 50-day average above 200-day")
    elif quote.trend == "down":
        score -= 2
        reasons.append("downtrend: 50-day average below 200-day")

    month = quote.month_change_pct
    if month is not None:
        if month > 5:
            score += 1
            reasons.append("strong 1-month momentum (%+.1f%%)" % month)
        elif month < -5:
            score -= 1
            reasons.append("weak last month (%+.1f%%)" % month)

    ytd = quote.ytd_change_pct
    if ytd is not None:
        if ytd > 15:
            score += 1
            reasons.append("strong year to date (%+.1f%%)" % ytd)
        elif ytd < -15:
            score -= 1
            reasons.append("weak year to date (%+.1f%%)" % ytd)

    off_high = quote.pct_off_high
    if off_high is not None and off_high >= -5:
        score += 1
        reasons.append("near its 52-week high")

    if r is not None:
        if r < 30:
            if quote.trend == "up":
                score += 1
                reasons.append(
                    "oversold (RSI %.0f) inside an uptrend — potential dip" % r)
            else:
                reasons.append(
                    "oversold (RSI %.0f) but no uptrend — falling-knife risk" % r)
        elif r > 70:
            score -= 1
            reasons.append("overbought (RSI %.0f) — stretched short-term" % r)

    label = WEAK_LABEL
    for threshold, name in LABELS:
        if score >= threshold:
            label = name
            break
    return {"score": score, "label": label, "rsi": r, "reasons": reasons}
