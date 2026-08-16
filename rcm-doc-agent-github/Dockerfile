FROM python:3.12-slim

WORKDIR /app

# Install deps first for better layer caching.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt && pip install --no-cache-dir streamlit>=1.30

COPY . .

EXPOSE 8501

# Default: run the document-extraction evaluation. A real API key IS required
# (there is no offline/mock mode) -- pass it via --env-file .env or -e.
# Override the command at run time, e.g.:
#   docker run --rm --env-file .env doc-agent python src/cli.py --doc data/docs/DOC-1000_denial_letter.txt
#   docker run --rm --env-file .env -p 8501:8501 doc-agent streamlit run ui/streamlit_app.py --server.address=0.0.0.0
CMD ["python", "-m", "src.docproc.evaluation.evaluate", "--docs", "data/docs"]
