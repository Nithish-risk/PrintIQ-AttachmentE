import tempfile
import uuid
from pathlib import Path


def create_session_dir() -> str:
    """Create a fresh working directory for this run.

    Uses the OS temp folder (outside any OneDrive-synced path) so uploaded
    files and extracted example images are NOT continuously synced by OneDrive
    or picked up by the Streamlit file watcher — both of which cause severe
    stalls when the project lives under a synced folder.
    """
    base = Path(tempfile.gettempdir()) / "printiq_sessions"
    base.mkdir(parents=True, exist_ok=True)
    session = base / uuid.uuid4().hex
    session.mkdir(parents=True, exist_ok=True)
    return str(session)


def save_upload(uploaded_file, session_dir) -> Path:
    """Persist a Streamlit UploadedFile into *session_dir* and return its path."""
    session_dir = Path(session_dir)
    session_dir.mkdir(parents=True, exist_ok=True)
    dest = session_dir / uploaded_file.name
    with open(dest, "wb") as f:
        f.write(uploaded_file.getbuffer())
    return dest
