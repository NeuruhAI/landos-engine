"""
app.insight — DS-07 AI INSIGHTS. The grounded brief generator.

FIREWALL: this package is NEURUH IP. The CRM consumes the brief over REST
(GET /v1/insight/{lead_id}) and only displays the result. The prompt, the
grounding rules, and the model routing never enter the CRM repo — same posture
as the scoring engine.

HONESTY LAW (the whole feature): every claim in a brief carries the field it
came from, returned in a `grounding` map. A field that is null produces NO
sentence. A thin record yields a SHORT brief — or an empty one. An empty brief
is a correct output. Fabrication is the only failure mode that matters.
"""
