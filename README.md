# Job-Board-Customizations
Experimenting with my career (and life...), and job search strategy, to create the ultimate job board for me. 

name: daily-tracker
on:
  schedule: [{cron: "0 13 * * *"}]
  workflow_dispatch:
permissions: {contents: write}
jobs:
  run:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with: {python-version: "3.12"}
      - run: pip install requests pyyaml
      - run: python run.py
      - run: |
          git config user.name tracker && git config user.email tracker@users.noreply.github.com
          git add jobs.sqlite docs/
          git commit -m "tracker $(date -u +%F)" || echo "no changes"
          git push
