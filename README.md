# WaterMap

Application Flask pour trouver des points d'eau et des toilettes à proximité d'un itinéraire GPX, KML ou Google Maps, avec comptes et données isolées par utilisateur.

## Branches et recette

Le développement et les corrections se font sur **`dev`**. La branche **`stag`**, issue de `dev`, sert à la recette de Yann sur le Raspberry Pi. Les corrections sont intégrées depuis `dev` dans `stag` pour être retestées. Après validation de Yann, fusionner la version validée de `stag` dans **`main`**, puis déployer `main` sur le VPS. Ne jamais déployer `dev` ou `stag` sur le VPS, ni ajouter à `main` des changements de `dev` non validés en recette. La production est servie sur https://watermap.dolisi.fr ; sa procédure est dans [deploy/ionos/README.md](deploy/ionos/README.md).

Le Compose à la racine ne gère que la recette. Les anciens conteneurs et volumes de production du RPi sont conservés séparément : ne pas utiliser `--remove-orphans` ou `down -v` pour nettoyer cette installation.

## Lancer la recette sur le RPi

Renseigner `.env` à partir de `.env.example`, avec une clé aléatoire d'au moins 32 caractères, et protéger le fichier en mode 600. Conserver la clé et les données de recette existantes lors d'une mise à jour. Une clé peut être générée avec `python3 -c 'import secrets; print(secrets.token_urlsafe(48))'` ; ne pas la publier.

```bash
cd /home/yann/watermap
git switch stag
docker compose config --quiet
docker compose up -d --build --wait --wait-timeout 90 watermap_dev
docker compose ps watermap_dev
```

Depuis le poste Windows de Yann, ouvrir **http://rpi4ng:8081**. Depuis le RPi, **http://localhost:8081** fonctionne également. La recette écoute sur toutes les interfaces IPv4 (`WATERMAP_DEV_BIND=0.0.0.0`) : Ethernet, Wi-Fi et Tailscale. Éviter une publication sur une seule adresse LAN : le nom `rpi4ng` peut être résolu vers une autre interface et provoquer des refus de connexion. `WATERMAP_DEV_PUBLIC_URL` doit correspondre à l'URL utilisée pour la recette ; sur cette installation, elle vaut `http://rpi4ng:8081`. Le service n'est pas destiné à être exposé directement sur Internet. Windows privilégiant les adresses IPv6 de ce nom, la recette du RPi publie aussi IPv6 avec `WATERMAP_DEV_BIND_V6=::`, après installation du [filtre LAN/Tailscale](deploy/rpi/README.md). Le défaut IPv6 reste `::1` pour les installations sans ce filtre. Pour limiter volontairement l’accès à un tunnel SSH, définir `WATERMAP_DEV_BIND=127.0.0.1` et `WATERMAP_DEV_BIND_V6=::1`.

L'image fonctionne avec l'UID/GID 10001 ; le code est monté en lecture seule et seuls les trois volumes de données et `/tmp` sont inscriptibles. Les anciens volumes créés par root nécessitent une sauvegarde puis une adaptation des droits à 10001 avant le premier démarrage. Cette adaptation a été faite sur les volumes dev du RPi le 4 novembre 2026 ; aucun volume de production n'a été modifié.

Les courriels réels sont désactivés en recette par défaut. Pour une recette SMTP autorisée, renseigner les variables `MAIL_*` et définir `WATERMAP_DEV_MAIL_SUPPRESS_SEND=false`, puis recréer le service. Les liens de récupération utilisent `WATERMAP_DEV_PUBLIC_URL`.

## Limites et fonctionnement

Les fichiers GPX et les téléchargements distants sont limités à 20 Mo, les tracés à 100 000 points et la distance de recherche à 0–5 000 mètres. Au plus deux tracés sont traités simultanément. Chaque compte dispose d'au plus 100 itinéraires ou 200 Mo ; une réserve de 512 Mo est conservée sur le disque. Les noms de pages en cours sont réservés pour empêcher l'écrasement lors d'importations simultanées.

La recherche calcule la distance aux segments complets du parcours sur une sphère, avec un index spatial et sans sous-échantillonnage. Les segments GPX/KML séparés ne sont pas reliés artificiellement. Si le routage Google Maps/OSRM échoue, l'application demande un fichier GPX plutôt que d'afficher un itinéraire en ligne droite. Les nouvelles cartes rendent uniquement les coordonnées et des textes échappés ; les métadonnées GPX ne sont pas exécutées dans le navigateur.

Conserver **un seul worker Gunicorn**, avec quatre threads : le suivi des tâches et les limites de requêtes sont en mémoire. Un redémarrage perd les statuts des traitements en cours. La configuration mémoire Docker nécessite le support des cgroups par le noyau ; Docker signale que cette limite n'est pas appliquée sur le RPi actuel.

La mise à jour des identifiants de session invalide les anciennes sessions et les anciens liens de réinitialisation. Les comptes, les mots de passe existants et les fichiers sont conservés. Il faut se reconnecter. Les anciennes cartes conservent leurs résultats de recherche historiques ; réimporter leurs GPX pour obtenir le calcul corrigé.

## Vérifications reproductibles

Les tests utilisent des bases temporaires et des courriels simulés. Ils ne modifient pas les données réelles. Le benchmark compare l'ancien calcul aux points échantillonnés avec la recherche sur les segments complets, sur les mêmes références.

```bash
cd /home/yann/watermap
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements-dev.txt
.venv/bin/python -m pytest -q
.venv/bin/bandit -q app.py remote_fetch.py gpx_utils.py spatial.py
.venv/bin/pip-audit -r requirements.lock --no-deps --disable-pip
.venv/bin/python -m tests.benchmark_spatial
# Vérification de l'image ARM64 construite, sans monter de données réelles.
docker compose build watermap_dev
docker run --rm --read-only --tmpfs /tmp:size=768m,mode=1777 --cap-drop ALL --security-opt no-new-privileges --pids-limit 128 -v /home/yann/watermap/deploy/ionos/smoke-test.py:/tmp/smoke-test.py:ro watermap:dev python /tmp/smoke-test.py
```

Le rapport de l'audit est dans [docs/audit-2026-11-04.md](docs/audit-2026-11-04.md).

## Administration et annonces

La branche de travail `dev-admin`, issue de `dev`, ajoute un espace réservé aux
administrateurs pour préparer des messages libres, des annonces d’indisponibilité
et des modèles de mise à jour modifiables. Les brouillons peuvent être publiés à
l’accueil et sur la page de connexion, puis envoyés individuellement par courriel
avec confirmation et suivi des tentatives. La simulation de recette ne contacte
pas SMTP. L’annonce d’indisponibilité ne bloque pas le service.

L’attribution des droits, le fonctionnement des deux canaux et la reprise des
envois sont décrits dans [docs/administration.md](docs/administration.md).

## GPX enrichi (recette)

Le bouton « GPX enrichi · eau et WC », présent sur la liste et le détail des
itinéraires, télécharge une copie du GPX avec les points déjà sélectionnés sur
la carte (même distance de recherche). Le tracé, ses segments, altitudes, dates,
extensions et points personnels sont conservés. Le GPX original reste inchangé.

Les ajouts sont des waypoints GPX avec un nom préfixé EAU ou WC, un type et un
symbole distincts. Leur affichage et leurs icônes sur le Edge après import
restent à valider sur l’appareil : cet export ne crée pas de course FIT ni
d’alertes Garmin. Les anciennes cartes contenant les listes JSON sont prises
en charge sans régénération ; sinon le téléchargement demande de recréer la
page. Les données cartographiques sont celles de la création de la carte.

Le bouton « Envoyer à Garmin Connect » prépare le même GPX enrichi pour le
partage natif du téléphone. Le partage est proposé uniquement si le navigateur
accepte ce fichier et si la page est sécurisée (HTTPS). Un second appui ouvre
le sélecteur d’applications ; Garmin Connect doit y être proposé par le
téléphone. L’import et l’envoi au Edge sont ensuite finalisés dans Connect.
Un téléchargement et des instructions restent disponibles si le partage est
indisponible ou annulé, notamment sur la recette HTTP. Aucune connexion de
compte Garmin ni publication via l’API Courses n’est effectuée.

## Import Garmin Connect (TCX)

Le bouton « Garmin (TCX) » et le partage mobile utilisent un TCX Course avec
des CoursePoints explicites. L’eau utilise le type Garmin Water ; les toilettes
utilisent Generic avec un nom WC, car le schéma TCX ne définit pas de type
toilettes. Les vraies coordonnées des POI sont conservées ; les points sont
ordonnés et associés au point de tracé le plus proche par leur horodatage.
Les noms courts respectent la limite du schéma et les noms complets restent
dans les notes. La chronologie TCX est indicative, à 18 km/h, sans donnée
d’activité réelle. Le GPX original et son export enrichi restent disponibles.

Cet export est prévu pour contourner l’échec de sauvegarde Connect observé
avec le GPX contenant des waypoints indépendants. La conformité XML est
testée ; l’enregistrement effectif dans Connect reste à valider sur le compte
de Yann.
