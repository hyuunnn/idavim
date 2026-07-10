"""idavim plugin entry point.

Only loads the real plugin when IDA is running interactively with a GUI
(the plugin needs Qt); otherwise a hidden no-op plugin is returned.
"""

import logging
import os

import ida_kernwin

logger = logging.getLogger("idavim")


def should_load():
    """Returns True if IDA 9.0+ is running interactively with a GUI."""
    if not ida_kernwin.is_idaq():
        return False

    if os.environ.get("IDA_IS_INTERACTIVE") != "1":
        return False

    kernel_version = tuple(
        int(part) for part in ida_kernwin.get_kernel_version().split(".") if part.isdigit()
    ) or (0,)
    # compare against (9,), not (9, 0): non-digit components are dropped
    # above, and a shortened tuple like (9,) would compare as OLDER than
    # (9, 0), wrongly rejecting a valid 9.x
    if kernel_version < (9,):
        logger.warning("IDA too old (must be 9.0+): %s", ida_kernwin.get_kernel_version())
        return False

    return True


if should_load():
    from idavim import idavim_plugin_t

    def PLUGIN_ENTRY():
        return idavim_plugin_t()

else:
    import ida_idaapi

    class idavim_nop_plugin_t(ida_idaapi.plugin_t):
        flags = ida_idaapi.PLUGIN_HIDE | ida_idaapi.PLUGIN_UNL
        wanted_name = "idavim disabled"
        comment = "idavim is disabled for this IDA environment"
        help = ""
        wanted_hotkey = ""

        def init(self):
            return ida_idaapi.PLUGIN_SKIP

    def PLUGIN_ENTRY():
        return idavim_nop_plugin_t()
