# fonts/

Vendored source assets, not generated output: IBM Plex Sans (400, 500, 600) and IBM Plex Mono
(400, 500), Latin subset, woff2.

| | |
|---|---|
| Licence | SIL Open Font License 1.1 (OFL-1.1); full text in `LICENSE-ibm-plex-sans.txt` and `LICENSE-ibm-plex-mono.txt`, copied unchanged |
| Copyright | 2019 IBM Corp. |
| Source | `@fontsource/ibm-plex-sans@5.3.0` and `@fontsource/ibm-plex-mono@5.3.0` on the npm registry (https://www.npmjs.com/package/@fontsource/ibm-plex-sans, https://www.npmjs.com/package/@fontsource/ibm-plex-mono; https://fontsource.org/fonts/ibm-plex-sans), files `files/ibm-plex-<family>-latin-<weight>-normal.woff2`, taken byte for byte |
| Upstream | https://github.com/IBM/plex |

The packages are not dependencies of the app; the files were copied once. To add a weight or a
subset, take the same file name from the same package version. The OFL requires the licence text to
travel with the fonts, so keep the two `LICENSE-*` files here.
