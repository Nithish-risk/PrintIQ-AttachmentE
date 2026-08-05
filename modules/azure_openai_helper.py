import json
from config.settings import settings

class AzureOpenAIHelper:
    def __init__(self):
        self.enabled = False
        self.client = None
        if not settings.PRINTIQ_USE_AOAI:
            return
        if settings.AZURE_OPENAI_ENDPOINT and settings.AZURE_OPENAI_KEY and settings.AZURE_OPENAI_DEPLOYMENT:
            try:
                from openai import AzureOpenAI
                self.client = AzureOpenAI(
                    azure_endpoint=settings.AZURE_OPENAI_ENDPOINT,
                    api_key=settings.AZURE_OPENAI_KEY,
                    api_version=settings.AZURE_OPENAI_API_VERSION,
                )
                self.enabled = True
            except Exception:
                self.enabled = False

    def explain_failure(self, rule_text: str, actual: str, status: str) -> str:
        if not self.enabled:
            return "AI explanation unavailable; Azure OpenAI is not configured."
        prompt = f"""
            You are assisting a QA tester validating a printed form PDF against Excel print rules.
            Explain the validation status concisely.
            Rule: {rule_text}
            Actual PDF output: {actual}
            Status: {status}
            Do not invent source data. If source data is needed, say so clearly.
            """
        try:
            resp = self.client.chat.completions.create(
                model=settings.AZURE_OPENAI_DEPLOYMENT,
                messages=[{"role":"system","content":"You are a precise QA validation assistant."},{"role":"user","content":prompt}],
                temperature=0.1,
                max_tokens=180,
            )
            return resp.choices[0].message.content.strip()
        except Exception as e:
            return f"AI explanation failed: {e}"

    def summarize_issues(self, issues: list[dict]) -> str:
        if not self.enabled or not issues:
            return ""
        safe = json.dumps(issues[:50], ensure_ascii=False)
        try:
            resp = self.client.chat.completions.create(
                model=settings.AZURE_OPENAI_DEPLOYMENT,
                messages=[{"role":"user","content":f"Summarize these print rule validation issues for a QA tester:\n{safe}"}],
                temperature=0.1,
                max_tokens=300,
            )
            return resp.choices[0].message.content.strip()
        except Exception:
            return ""

    def repair_rule(self, rule_fields: dict, issue: str) -> dict:
        """Return a corrected copy of *rule_fields* fixing the detected *issue*.

        The model may ONLY fix typos/contradictions while preserving the rule's
        original meaning as closely as possible. It must return a JSON object
        with the same keys it was given (a subset of the PrintRule text fields).
        On any failure the original ``rule_fields`` is returned unchanged.
        """
        if not self.enabled:
            return rule_fields
        keys = list(rule_fields.keys())
        prompt = f"""
You are correcting a single print-rule row from an Excel spec for a vital-records
form. A validator flagged this issue: {issue}

Fix ONLY typos, misspellings, or internal contradictions (e.g. a Party B rule
that mistakenly references Party A). Preserve the original meaning and wording as
closely as possible. Do NOT invent new instructions or change field semantics.

Return ONLY a JSON object with exactly these keys: {keys}
Original rule fields:
{json.dumps(rule_fields, ensure_ascii=False)}
"""
        try:
            resp = self.client.chat.completions.create(
                model=settings.AZURE_OPENAI_DEPLOYMENT,
                messages=[
                    {"role": "system", "content": "You are a precise text-correction assistant. Respond with JSON only."},
                    {"role": "user", "content": prompt},
                ],
                temperature=0.0,
                max_tokens=500,
                response_format={"type": "json_object"},
            )
            content = resp.choices[0].message.content
            corrected = json.loads(content) if content else {}
            if not isinstance(corrected, dict):
                return rule_fields
            # Only accept the keys we asked for; keep originals for anything missing.
            out = dict(rule_fields)
            for k in keys:
                if k in corrected and isinstance(corrected[k], (str, type(None))):
                    out[k] = corrected[k]
            return out
        except Exception:
            return rule_fields
