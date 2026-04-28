# WaterMap - Serveur de points d'eau et toilettes

Ce projet est un serveur web Python/Flask qui permet de trouver les points d'eau et les toilettes à proximité d'un tracé GPX ou d'un itinéraire Google Maps.

## 🚀 Utilisation avec Docker

1. **Construire l'image** :

```bash
docker build -t watermap .
```

2. **Lancer le conteneur** :

```bash
docker run -p 8080:80 \
  -e SECRET_KEY="choisissez_une_cle_aleatoire_tres_longue" \
  watermap
```

3. Ouvrez `http://localhost:8080` dans votre navigateur.

4. **Premier accès** : Cliquez sur "S'inscrire" pour créer votre compte local.

## ✨ Fonctionnalités
*   **Gestion Utilisateurs** : Inscription et connexion par email/mot de passe. Les données sont isolées par utilisateur.
*   **Sécurité** : Hachage des mots de passe (PBKDF2) et système de réinitialisation via jetons sécurisés.
*   **Import multi-source** : Téléversement de fichiers GPX ou lien direct Google Maps (Itinéraire ou My Maps).
*   **Routage intelligent** : Calcul automatique du tracé réel pour les liens Google Maps via OSRM.
*   **Calcul de proximité** : Trouve les points d'eau et toilettes à moins de 500m du tracé.
*   **Interface interactive** : Carte Leaflet avec marqueurs personnalisés et Street View.
