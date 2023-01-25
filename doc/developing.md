## Code formatting

The code is checked/formatted with the following tools:

- [black](https://github.com/psf/black), usage: `black .`
- [mypy](http://mypy-lang.org/), usage: `mypy --strict banjo/` from higher directory. I'm not sure how to run it from
  root of repo.

Black does not [group](https://www.python.org/dev/peps/pep-0008/#imports) and sort imports. I either manually do that or
use a [vim plugin](https://github.com/fisadev/vim-isort) for [isort](https://github.com/timothycrosley/isort) after
selecting lines in visual mode. I don't just run isort because it disagrees with black on formatting and breaks comments
used for ignoring mypy types.

I occasionally run [flake8](https://flake8.pycqa.org/en/latest/) because it catches some things that black doesn't, like
unused imports.

## Binja plugin development

I don't have anything fancy to recommend here, but some tips for first-time Binary Ninja plugin developers:

1. Launch binja from a terminal with `binaryninja -de file.dex`. debug messages don't appear in the gui
2. You have to completely restart binja for BinaryView changes to take effect
3. Don't ctrl-c in the terminal. It gets weird
4. In binja use ctrl-p to do everything

## Testing

This could use a lot of improvement. See https://github.com/CarveSystems/banjo/issues/21 to talk about testing output.

There are some unittest classes for some low-level functions. Use `python -m unittest android.dex`
and `python -m unittest android.smali` to run those.
