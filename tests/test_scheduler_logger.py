"""Regression: scheduler router must define a module `logger`.

The job create/update endpoints call `logger.warning(...)` inside their
skill-generation except handlers. The module previously never imported logging
or defined `logger`, so when skill-gen dispatch failed the except handler itself
raised `NameError: name 'logger' is not defined`, masking the real error.
"""

import logging


def test_scheduler_module_has_logger():
    import apps.api.routers.scheduler as scheduler

    assert hasattr(scheduler, "logger"), "scheduler router must define `logger`"
    assert isinstance(scheduler.logger, logging.Logger)
