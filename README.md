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

Ouvrir **http://localhost:8081 sur le RPi**. Le service écoute uniquement sur la boucle locale. Pour y accéder depuis un autre poste, utiliser un tunnel SSH ou définir explicitement `WATERMAP_DEV_BIND` sur l'adresse LAN/Tailscale du RPi. Dans ce cas, `WATERMAP_DEV_PUBLIC_URL` doit correspondre à l'URL utilisée pour la recette. Le service n'est pas destiné à être exposé directement sur Internet.

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
