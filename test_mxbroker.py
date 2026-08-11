"""Self-check for the response parser - the only non-trivial logic. No CubeMX needed."""

from mxbroker import clean, done_status

# the two terminators seen on the wire
assert done_status("67768 OK\r") == "OK"
assert done_status("2 KO") == "KO"
assert done_status("74 OK\r\n") == "OK"
# ...and things that only look like them
assert done_status("OK") is None
assert done_status("MX> 67768 OK") is None
assert done_status("Loading OK for 1 mcu") is None
assert done_status("") is None

RAW = """MX> log4j user configuration file not found: C:\\Users\\x/.stm32cubemx/log4j2.xml
2026-08-12 00:02:04,993 [INFO] MicroXplorer:468 - Change Database Path :
2026-08-12 00:33:04,391 [INFO] IPUIPlugin:83 - create IPUIPlugin
Picked up JAVA_TOOL_OPTIONS: -Dfoo
Could not open/create prefs root node
java.util.prefs.WindowsPreferences
PA0 : ADC1_IN0
2026-08-12 00:33:04,391 [INFO] IP:1882 - I2C1 | ClockSpeed = 100000

67768 OK
MX> """
out = clean(RAW)
# the IP logger carries 'get mode_param_list' results; every other timestamped line is noise
assert out.splitlines() == ["PA0 : ADC1_IN0",
                            "I2C1 | ClockSpeed = 100000",
                            "67768 OK"], out
assert clean("MX> Bye bye") == "Bye bye"

print("ok")
