# spotter-naip-cutter
Cuts public-domain aerial imagery into PMTiles tile archives.

`cut_cell.py` takes one z12 cell, finds USDA NAIP imagery for it on Microsoft Planetary Computer, reads only the pixels it needs from the remote COGs, renders z12 to z17 as 512 px WebP tiles, writes one PMTiles v3 archive and uploads it to R2. It uses the newest year that covers the cell and adds older years only where the newest leaves a gap.

## Environment

| Variable | Use |
| --- | --- |
| `OD1_CELL` | Required. The cell as `12/x/y`. |
| `R2_ACCESS_KEY_ID`, `R2_SECRET_ACCESS_KEY`, `R2_ENDPOINT` | Required unless `--dry-run` is set. |
| `RUNNER_TEMP` | Directory for the default error file. Falls back to the system temp directory. |

## Flags

- `--dry-run --out PATH` writes the archive to PATH and skips the upload. The two flags go together.
- `--err PATH` sets the error file. The default is a randomly named file. Tracebacks and warnings go only to this file, which can contain cell details, so treat it as private.

## Status

Stdout gets exactly one line and stderr gets nothing.

| Word | Exit | Meaning |
| --- | --- | --- |
| `ok` | 0 | Archive written or uploaded. |
| `none` | 0 | No imagery covers the cell, nothing written. |
| `fail:E1` | 1 | Bad input or flags. |
| `fail:E2` | 1 | STAC search failed. |
| `fail:E3` | 1 | Reading imagery failed. |
| `fail:E4` | 1 | Rendering or writing failed. |
| `fail:E5` | 1 | Upload failed or R2 environment incomplete. |

## Tests

    pip install -r requirements.txt pytest
    pytest
