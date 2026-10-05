"""Retired experiment for complete-brief preparation workers.

Historical reports describe their original implementation and are preserved.
Owner briefs now omit full preparation and read one snapshot. Use the current
Stage 9 business, browser and evidence tools for the retained service reads.
"""

import argparse


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.error(
        "Retired: complete-brief preparation workers no longer have a consumer. "
        "Preserve historical reports; use current Stage 9 business/browser/evidence tools."
    )


if __name__ == "__main__":
    main()
