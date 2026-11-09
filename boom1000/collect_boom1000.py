name: Collecte Boom 1000

on:
  workflow_dispatch:
    inputs:
      tick_count:
        description: Nombre de ticks à collecter
        required: true
        default: "100000"

permissions:
  contents: read

jobs:
  collect:
    runs-on: ubuntu-latest
    timeout-minutes: 15

    steps:
      - name: Télécharger le dépôt
        uses: actions/checkout@v4

      - name: Installer Python
        uses: actions/setup-python@v5
        with:
          python-version: "3.12"

      - name: Installer les dépendances
        run: pip install -r boom1000/requirements.txt

      - name: Collecter les données
        env:
          TICK_COUNT: ${{ inputs.tick_count }}
        run: python boom1000/collect_boom1000.py

      - name: Sauvegarder le CSV
        uses: actions/upload-artifact@v4
        with:
          name: boom1000-ticks
          path: boom1000_ticks.csv
          retention-days: 14
