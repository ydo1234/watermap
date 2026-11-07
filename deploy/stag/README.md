# Recette HTTPS sur le VPS

Installation autorisée par Yann le 7 novembre 2026 : la branche stag peut être
déployée dans /opt/watermap-stag, distinct de /opt/watermap (production main).
Domaine : https://stag.watermap.dolisi.fr, DNS A vers 82.165.183.204.

Utiliser une clé SECRET_KEY propre à la recette, FLASK_ENV=development,
WATERMAP_PUBLIC_URL=https://stag.watermap.dolisi.fr, WATERMAP_INSTANCE=stag,
MAIL_REDIRECT_TO=ydolisi@gmail.com et MAIL_SUPPRESS_SEND=false. Tous les
destinataires À/Cc/Cci sont remplacés par cette adresse, avec objet [STAG].
Les seuls paramètres SMTP nécessaires peuvent être repris du serveur existant,
sans copier la clé de session ni les données de production. Les données de recette
proviennent exclusivement du Raspberry ; ne jamais remplacer celles de production.

Construire depuis un export propre de stag avec le Dockerfile IONOS et ce
fichier wsgi.py dans source/. Le manifeste doit identifier la branche et son
commit exact. Compose utilise un projet et un nom de service distincts ; le
port 8082 est local seulement. Le nom réseau du service ne doit jamais être
app, pour éviter toute confusion avec l’amont de production dans Caddy.

Ajouter le fragment Caddyfile.fragment à la configuration du proxy existant
après sauvegarde, puis valider et recharger Caddy sans recréer la production.
Conserver ce fragment lors des futurs déploiements main ; une préparation de
release main conserve sa configuration historique et ne doit pas l’écraser
aveuglément.

Avant une mise à jour, sauvegarder les données et la configuration de recette,
conserver l’image précédente puis construire et activer uniquement stag_app.
Pour revenir en arrière, restaurer le code/image précédents en conservant les
volumes courants. Garder les sauvegardes privées.
