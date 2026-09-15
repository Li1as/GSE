"""Explicit ehall transaction adapters.

Adapters describe page fields.  They do not own task persistence and, before
stage 4C, expose no browser fill or submit operation.
"""

from .timetable_withdrawal import TimetableWithdrawalAdapter


def default_adapters():
    adapter = TimetableWithdrawalAdapter()
    return {adapter.name: adapter}

