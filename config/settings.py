import os
from dotenv import load_dotenv

load_dotenv()

class Settings:
    AZURE_DOC_INTELLIGENCE_ENDPOINT = os.getenv("AZURE_DOC_INTELLIGENCE_ENDPOINT")
    AZURE_DOC_INTELLIGENCE_KEY = os.getenv("AZURE_DOC_INTELLIGENCE_KEY")

    AZURE_OPENAI_ENDPOINT = os.getenv("API_ENDPOINT")
    AZURE_OPENAI_KEY = os.getenv("API_KEY")
    AZURE_OPENAI_DEPLOYMENT = os.getenv("AZURE_OPENAI_DEPLOYMENT")
    AZURE_OPENAI_API_VERSION = os.getenv("API_VERSION", "2024-02-15-preview")

    PRINTIQ_USE_AOAI = os.getenv("PRINTIQ_USE_AOAI", "true").lower() == "true"
    PRINTIQ_MASK_PII = os.getenv("PRINTIQ_MASK_PII", "true").lower() == "true"

    # When true, run the Step 10 LLM reconstruction pass over the geometry
    # structured_fields (refines section/subsection/key only). Defaults off so
    # the deterministic pipeline works with no OpenAI credentials/cost.
    PRINTIQ_USE_STEP10 = os.getenv("PRINTIQ_USE_STEP10", "false").lower() == "true"

settings = Settings()
