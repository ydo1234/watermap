# Administration et messages aux utilisateurs (WM-001)

Développement sur `dev-admin`, issue de `dev`, dans `/home/yann/watermap-dev-admin`.
La recette existante reste sur `stag` dans `/home/yann/watermap`. Après intégration
dans `dev`, la nouvelle version devra passer par `stag` et être validée avant
toute promotion dans `main` ou mise en production.

## Accès administrateur

L’espace `/admin` nécessite un compte connecté et des droits explicites. Aucun
compte existant ou nouvel inscrit ne devient administrateur automatiquement.
Les droits sont vérifiés à chaque requête et leur retrait prend effet dès la
requête suivante, sans attendre une reconnexion.

Depuis l’environnement de l’instance qui contient cette version de l’application,
avec ses variables et ses données habituelles :

```bash
# Choisir l’adresse d’un compte déjà inscrit sur cette instance.
python -m flask --app app admin-user adresse@example.org
# Pour retirer les droits, lancer cette commande seulement si nécessaire :
# python -m flask --app app admin-user adresse@example.org --revoke
```

Un lien « Administration » apparaît ensuite dans l’accueil de ce compte. Les
nouvelles tables sont créées au démarrage ; la table des utilisateurs, les mots
de passe, les sessions et les fichiers existants sont conservés.

## Préparer et diffuser une annonce

1. Rédiger un message libre ou sélectionner un modèle d’indisponibilité ou de
   mise à jour programmée. Pour une mise à jour, renseigner date, heure de Paris
   et durée estimée, puis cliquer sur « Remplir avec le modèle ».
2. Modifier le texte à volonté et enregistrer le brouillon. Il reste privé.
3. Vérifier la prévisualisation, puis publier dans l’application, envoyer par
   courriel, ou utiliser les deux canaux indépendamment.

Les annonces publiées apparaissent à l’accueil et sur la page de connexion,
y compris avant identification. Le texte est échappé et ne permet pas de HTML.
Une annonce peut être retirée de l’affichage. Le contenu d’une annonce diffusée
est figé ; préparer une nouvelle annonce pour apporter une correction.

L’indisponibilité est une **annonce**, sans blocage du service. La date est une
information dans le message, sans publication programmée ni mode maintenance.

## Courriels et suivi

Chaque envoi nécessite une confirmation explicite dans l’interface. Les
destinataires sont exclusivement les utilisateurs de la base de l’instance
courante, figés lors du premier envoi de l’annonce. Aucun accès à une autre
instance n’est effectué. Les courriels sont individuels, sans copie collective,
et incluent le lien défini par `WATERMAP_PUBLIC_URL`.

`MAIL_SUPPRESS_SEND=true` active une simulation sans aucun appel SMTP. C’est
le réglage par défaut de la recette. Le suivi distingue les envois simulés des
envois réels. Une annonce préparée pour un mode ne peut pas être réexpédiée dans
l’autre mode : créer une nouvelle annonce après changement de configuration.

Le suivi est conservé dans la base SQLite et distingue « En attente », « En
cours », « Envoyé », « Simulé » et « À vérifier ». Actualiser la page pour voir
la progression. Le bouton de reprise ne traite que les destinataires encore en
attente : il ne répète ni les succès, ni les simulations, ni les tentatives dont
le résultat SMTP est incertain. Après interruption du processus, une tentative
restée « En cours » nécessite également une vérification manuelle. Les
tentatives incertaines n’ont pas de bouton de réexpédition automatique.

Les envois utilisent la file bornée de courriels existante. Conserver la
configuration à un worker Gunicorn et quatre threads décrite dans le README.
