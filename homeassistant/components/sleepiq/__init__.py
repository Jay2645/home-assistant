"""Support for SleepIQ from SleepNumber."""
from contextlib import redirect_stdout
from datetime import timedelta
from io import StringIO
import logging
from requests.exceptions import ConnectionError

from sleepyq import Sleepyq
import voluptuous as vol

from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.exceptions import PlatformNotReady, UnknownUser
from homeassistant.helpers import discovery
import homeassistant.helpers.config_validation as cv
from homeassistant.helpers.entity import Entity
from homeassistant.util import Throttle

DOMAIN = "sleepiq"

MIN_TIME_BETWEEN_UPDATES = timedelta(seconds=30)

IS_IN_BED = "is_in_bed"
SLEEP_NUMBER = "sleep_number"
SENSOR_TYPES = {SLEEP_NUMBER: "SleepNumber", IS_IN_BED: "Is In Bed"}

LEFT = "left"
RIGHT = "right"
SIDES = [LEFT, RIGHT]

_LOGGER = logging.getLogger(__name__)

DATA = None

CONFIG_SCHEMA = vol.Schema(
    {
        vol.Required(DOMAIN): vol.Schema(
            {
                vol.Required(CONF_USERNAME): cv.string,
                vol.Required(CONF_PASSWORD): cv.string,
            }
        )
    },
    extra=vol.ALLOW_EXTRA,
)


def setup(hass, config):
    """Set up the SleepIQ component.

    Will automatically load sensor components to support
    devices discovered on the account.
    """
    global DATA

    username = config[DOMAIN][CONF_USERNAME]
    password = config[DOMAIN][CONF_PASSWORD]
    client = Sleepyq(username, password)
    try:
        DATA = SleepIQData(client)
        DATA.update()
    except (ConnectionError, UnknownUser) as e:
        raise PlatformNotReady(e)

    discovery.load_platform(hass, "sensor", DOMAIN, {}, config)
    discovery.load_platform(hass, "binary_sensor", DOMAIN, {}, config)

    return True


class SleepIQData:
    """Get the latest data from SleepIQ."""

    def __init__(self, client: Sleepyq):
        """Initialize the data object."""
        self._client = client
        self.beds = {}

        self.update()

    @Throttle(MIN_TIME_BETWEEN_UPDATES)
    def update(self):
        """Get the latest data from SleepIQ."""
        # The client prints to stdout occassionally for warnings.
        # We use redirect_stdout to intercept those calls and pipe them
        # to our logger instead for better context.
        client_output = StringIO()
        try:
            with redirect_stdout(client_output):
                self._client.login()
                beds = self._client.beds_with_sleeper_status()
        except ConnectionError:
            # Clear bed data due to bad endpoint.
            self.beds = {}
            raise
        except AttributeError:
            # This can happen if the SleepIQ API endpoint is unavailable.
            self.beds = {}
            raise ConnectionError("SleepIQ API unavailable")
        except ValueError:
            self.beds = {}
            message = """
                SleepIQ login failed. Double-check your username and password.
            """
            raise UnknownUser(message)
        finally:
            # Print any warnings that the client generated
            warning_output = client_output.getvalue()
            if len(warning_output) > 0:
                _LOGGER.warning(client_output.getvalue())

        if len(self.beds) == 0:
            # Either connected for first time or reconnected after disconnect.
            _LOGGER.debug("Connected to SleepIQ.")

        # Update the bed list
        self.beds = {bed.bed_id: bed for bed in beds}


class SleepIQSensor(Entity):
    """Implementation of a SleepIQ sensor."""

    def __init__(self, sleepiq_data: SleepIQData, bed_id: int, side: str):
        """Initialize the sensor."""
        self._bed_id = bed_id
        self._side = side
        self.sleepiq_data = sleepiq_data
        self.side = None
        self.bed = None

        self._available = False
        self._attributes = {}

        # added by subclass
        self._name = None
        self.type = None

    @property
    def name(self):
        """Return the name of the sensor."""
        if self.bed == None or self.side == None:
            # Bed is unavailable -- use our known ID instead
            return self.unique_id
        else:
            sleeper_name = self.side.sleeper.first_name
            return f"Sleep Number {self.bed.name} {sleeper_name} {self._name}"
    
    @property
    def unique_id(self):
        """Return a unique ID."""
        return f"Sleep Number {self._bed_id} {self._side} {self._name}"

    @property
    def device_info(self):
        return {
            'identifiers': {
                (DOMAIN, self.unique_id)
            },
            'name': self.bed.name,
            'bed_id': self._bed_id,
            'mac_address': self.bed.mac_address,
            'model': self.bed.model,
            'sku': self.bed.sku,
            'generation': self.bed.generation,
            'purchase_date': self.bed.purchase_date,
            'registration_date': self.bed.registration_date,
            'size': self.bed.size,
            'side': self._side,
        }

    @property
    def available(self) -> bool:
        """Return True if entity is available."""
        return self._available

    @property
    def assumed_state(self) -> bool:
        """Return True if unable to access real state of the entity."""
        return not self._available

    @property
    def device_state_attributes(self):
        """Return the state attributes."""
        return self._attributes

    def update(self):
        """Fetch the latest data from SleepIQ."""
        try:
            # Call the API for new sleepiq data. Each sensor will re-trigger 
            # this same exact call, but that's fine. We cache results for a 
            # short period of time to prevent hitting API limits.
            self.sleepiq_data.update()
        except ConnectionError:
            # Wasn't able to update; mark as unavailable and use cached data
            self._available = False
            return

        self._available = True

        self.bed = self.sleepiq_data.beds[self._bed_id]
        self.side = getattr(self.bed, self._side)

        self._attributes["bed_info"] = self.device_info
        self._attributes["alerts"] = {
            "alert_id": self.side.alert_id, 
            "message": self.side.alert_detailed_message,
        }
        self._attributes["sleeper"] = self.side.sleeper.first_name
