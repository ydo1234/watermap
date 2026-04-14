FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY . ./

# Créer les dossiers pour la persistance des données
RUN mkdir -p pages uploads

EXPOSE 80
CMD ["gunicorn", "-w", "1", "-b", "0.0.0.0:80", "app:app"]
