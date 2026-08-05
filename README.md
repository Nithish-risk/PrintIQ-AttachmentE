# printiq

**printiq** is a Streamlit-based AI-assisted validation tool for checking automation-generated printform PDFs against uploaded Excel Print Product Rules.

## What it does

- Upload an Excel Print Product Rules workbook.
- Upload a generated printform PDF.
- Suggest the matching Excel sheet using PDF/Excel content similarity.
- Allow manual sheet override.
- Parse print rules from the selected sheet.
- Extract PDF text, layout, tables, and selection marks using Azure Document Intelligence.
- Validate static text, date formats, labels, visible business rules, layout regions, and checkbox/selection marks.
- Flag Excel rule issues separately.
- Generate:
  - Annotated PDF
  - `validation_results.json`
  - `validation_summary.xlsx`

## Supported initial print product families

- Marriage Application
- Certificate of Marriage
- Court Ordered Amendment
- Officiant Affidavit

The code is generic enough to support additional sheets as long as the workbook follows the same Attachment E-style rule structure.

## Environment variables

Create environment variables locally or in Azure Web App application settings:

```bash
AZURE_DOC_INTELLIGENCE_ENDPOINT=https://<your-resource>.cognitiveservices.azure.com/
AZURE_DOC_INTELLIGENCE_KEY=<your-key>
AZURE_OPENAI_ENDPOINT=https://<your-openai-resource>.openai.azure.com/
AZURE_OPENAI_KEY=<your-key>
AZURE_OPENAI_DEPLOYMENT=<deployment-name>
AZURE_OPENAI_API_VERSION=2024-02-15-preview
PRINTIQ_USE_AOAI=true
PRINTIQ_MASK_PII=true
```

## Local run

```bash
python -m venv .venv
source .venv/bin/activate  # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env
streamlit run app.py
```

## Azure Web App Linux deployment

1. Push this repo to GitHub.
2. Create an Azure Web App for Linux with Python 3.11.
3. Connect GitHub deployment.
4. Add the environment variables under **Configuration > Application settings**.
5. Set startup command:

```bash
bash startup.sh
```

## Container deployment fallback

If PyMuPDF/OpenCV dependencies fail under normal Web App deployment, use the included `Dockerfile` with Azure App Service for Containers or Azure Container Apps.

## Notes

- Uploaded files are handled temporarily per session.
- Source data is not required. Rules that need source data are marked `NEEDS_SOURCE_DATA` instead of being incorrectly failed.
- PII such as SSNs is masked in reports by default when `PRINTIQ_MASK_PII=true`.
