"""The details of a connector or a layout key: each binding, with the objects that the user can view.

The device view pages put each Details in a ``<template>``; the page script
shows it in one dialog.
"""

from dataclasses import dataclass

from .components import AMBIGUOUS, ComponentState, KeyBinding

FOUND = "found"
MISSING = "missing"
# What a click on a connector opens: its one component, the chooser, or the unresolved details.
SINGLE = "single"
SEVERAL = "several"
UNRESOLVED = "unresolved"
# A generated id starts with "-" after the prefix; an author id starts with a letter.
_DETAILS = "-details-"


def resolution(statuses) -> str:
    """Return SINGLE for one found binding, SEVERAL for two or more, else UNRESOLVED."""
    statuses = list(statuses)
    if not statuses or any(status != FOUND for status in statuses):
        return UNRESOLVED
    return SINGLE if len(statuses) == 1 else SEVERAL


@dataclass(frozen=True)
class Entry:
    """One binding: a found component, a missing name, or the candidates of an ambiguous key."""

    label: str
    status: str
    candidates: tuple[ComponentState, ...] = ()

    @property
    def found(self):
        return self.status == FOUND

    @property
    def missing(self):
        return self.status == MISSING

    @property
    def state(self) -> ComponentState | None:
        return self.candidates[0] if self.found else None


@dataclass(frozen=True)
class Details:
    ident: str
    title: str
    entries: tuple[Entry, ...]

    @property
    def resolution(self) -> str:
        return resolution(entry.status for entry in self.entries)

    @property
    def several(self):
        return self.resolution == SEVERAL

    @property
    def unresolved(self):
        return self.resolution == UNRESOLVED


def details_id(prefix, key) -> str:
    return f"{prefix}{_DETAILS}{key}"


def key_details(prefix, binding: KeyBinding) -> Details:
    """Return the details of a layout key: its one component, or the candidates that share the key."""
    state = binding.state
    if state is None:
        return Details(
            details_id(prefix, binding.key),
            binding.key,
            (Entry(binding.key, AMBIGUOUS, tuple(binding.candidates)),),
        )
    entry = Entry(state.label, FOUND, (state,))
    return Details(details_id(prefix, binding.key), state.component.name, (entry,))
