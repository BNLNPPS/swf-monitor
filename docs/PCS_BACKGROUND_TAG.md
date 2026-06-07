# PCS Background Tag (`k`)

The fifth PCS tag type, prefix `k`. It names a background configuration —
beam-gas, synchrotron radiation, or other pre-generated overlay samples — as a
locked, reusable tag, independent of any physics signal, taking its place in the
dataset name alongside `p`, `e`, `s`, `r`.

## Purpose

Background is generated independently of any physics signal and mixed into many.
It is not a physics process tied to a particular signal — that
independence is why it is a tag type of its own rather than a field on the
physics or evgen tag. One background definition is reused across every signal it
overlays.

Its configuration is currently spread across `EvgenTag` (`signal_freq`,
`bg_tag_prefix`, `bg_files`) and `ProdConfig` (`bg_mixing`, `bg_cross_section`,
`bg_evtgen_file`), unnamed and unversioned. The `k` tag gathers it into one
locked record and gives it a place in the dataset identity, alongside those
fields rather than replacing them.

## Why `k`

A distinct letter keeps physics tags decimal, rather than folding a background
digit into `p` and forcing rethinking numbering (e.g. hex). `b` is already the block suffix
(`.b1`, `.b2`).

## Schema

In `pcs/schemas.py`:

- Required: `background_type` (`BEAMGAS`, `SYNRAD`).
- Optional: `source_sample`, `cross_section`, `signal_freq`, `evtgen_file`,
  `bg_tag_prefix`, `beam_energy_electron`, `beam_energy_hadron`, `beam_species`,
  `notes`.

The beam energies and species the background was generated for live here, on the
background tag; background is beam-dependent.

## Behavior

`k` is structurally identical to `e`/`s`/`r`: sequential labels `k1`, `k2`, …,
`draft → locked`, creator-owned, no categories. A dataset carries at most one
background tag, set independently of its physics tag.

## The no-signal physics tag

Every dataset names a physics tag. A standalone background sample has no signal,
so it names `p6001` — a single physics tag in a new category 6, created once and
locked, all parameters blank. It means "no physics signal; see the background
tag." A dataset that mixes background into a real signal keeps that signal's
physics tag and adds a `k`.

## Dataset name

`k` appends after reco, before the Rucio block:

```
{scope}.{detector_version}.{detector_config}.{p}.{e}.{s}.{r}.{k}   →   DID …{k}.b{N}
```

The segment is present only when the dataset carries a background.
