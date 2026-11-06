# Cazy Studio — Updates and Releases

Cazy Studio is an experimental local Windows video editor. Version 0.3 adds an in-app **Update Center**.

## Publishing updates

The latest published (non-draft, non-prerelease) GitHub release is the update source for the desktop app.

1. Open **https://github.com/cazymf/Cazy-Studio/releases/new**
2. Create a new tag like `v0.3.0`.
3. Name the release `Cazy Studio v0.3.0`.
4. Upload **`Cazy_Studio_v0_3_0_Update.zip`** as the release asset, then click **Publish release**.

For `v0.4.0`, the asset must be named `Cazy_Studio_v0_4_0_Update.zip`, and so on. Each ZIP contains `update_manifest.json`, with per-file SHA-256 hashes. GitHub supplies a SHA-256 digest for the release asset, which the app verifies before installation.

The Update Center updates **app files only** and backs up the previous version. Projects, exported videos, downloaded `Addons`, local `.venv` Python environment, and user-specific settings are retained. After installation it restarts Cazy Studio. Use **Restore Previous** or `Restore_Previous_Version.bat` to roll back.

The *first* upgrade from v0.2 to v0.3 is a manual download. It includes `Upgrade_v0_2_to_v0_3.bat`; run it from the extracted package and point it at your existing installation.

## Important

This repo does not yet contain the release binaries. Cazy Studio cannot offer an update until the correctly named ZIP has been uploaded and a release published.

The GitHub integration used to make these notes can edit repository text files, but does not currently expose a release-asset upload action. Binaries still need to be uploaded through the GitHub Releases webpage.

Project maintainer: cazymf
