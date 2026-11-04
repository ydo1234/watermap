FROM python:3.12-slim@sha256:b18b261e1b4974b4f321451ad5251ded85abb22c2b8b262da527dda1d031ab30
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_DISABLE_PIP_VERSION_CHECK=1
RUN groupadd --gid 10001 watermap && useradd --uid 10001 --gid 10001 --no-log-init --create-home watermap
WORKDIR /app
COPY requirements.lock ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt
COPY --chown=10001:10001 . ./
RUN mkdir -p db pages uploads && chown -R 10001:10001 db pages uploads
USER 10001:10001
EXPOSE 8000
CMD ["gunicorn", "--no-control-socket", "--workers", "1", "--threads", "4", "--timeout", "120", "--bind", "0.0.0.0:8000", "--error-logfile", "-", "app:app"]
