# RouteMind user photos

Photos of places that RouteMind users took **at the place** and chose to share, published after
moderation. Each photo is an asset of a `photos-NNNN` release of this repository.

- **Licence:** CC BY-SA 4.0. Each photo is credited in the app with the name its author chose
  («مستخدم RouteMind» / "RouteMind user" by default).
- **Privacy:** before a photo is published, every face and licence plate is blurred and all metadata
  (EXIF, location) is removed. Uploads carry no account and no personal data.
- **Moderation:** `.github/workflows/moderate.yml` runs every three hours: a nudity detector, the
  blurring, a size limit, at most ten photos per place. Photos waiting for it sit in the draft release
  `pending`, which is not public. Three different users flagging a photo in the app hide it, and the
  next run deletes its files.
- **Removal requests:** open an issue in this repository with the photo's address; it is deleted.

This repository holds no code of the app. `moderate.py` and the workflow are generated from the RouteMind
app repository (`tools/ugc/`); edit them there.
