# Serveur web Docker

Ce projet contient un petit serveur web Python/Flask prêt à être lancé dans un conteneur Docker.

## Utilisation

1. Construire l'image Docker :

```bash
docker build -t webserver .
```

2. Lancer le conteneur :

```bash
docker run -p 8080:80 webserver
```

3. Ouvrir `http://localhost:8080` dans votre navigateur.

4. Sur la page principale, téléversez un fichier GPX.

5. Une nouvelle page HTML sera créée avec le nom du fichier GPX et apparaîtra dans la liste.

6. Vous pouvez ouvrir ou supprimer les pages générées depuis l’interface.
