#
# For the full copyright and license information, please see the
# AUTHORS and LICENSE files distributed with this source code, or
# at https://www.sourcefabric.org/superdesk/license

import logging
import json
from datetime import timedelta

import gspread
from gspread import Cell
from dateutil.parser import parse
from oauth2client.service_account import ServiceAccountCredentials
from pytz import timezone
from pytz.exceptions import UnknownTimeZoneError
from tzlocal import get_localzone

import superdesk
from superdesk.errors import IngestApiError, ParserError, SuperdeskIngestError
from superdesk.io.feeding_services import FeedingService
from superdesk.io.registry import register_feeding_service
from superdesk.metadata.item import CONTENT_STATE, GUID_NEWSML, ITEM_STATE
from superdesk.metadata.utils import generate_guid

logger = logging.getLogger(__name__)


class IngestSpreadsheetError(SuperdeskIngestError):
    _codes = {
        15100: "Missing permission",
        15200: "Quota limit",
        15300: "Invalid credentials"
    }

    @classmethod
    def SpreadsheetPermissionError(cls, exception=None, provider=None):
        return IngestSpreadsheetError(15100, exception, provider)

    @classmethod
    def SpreadsheetQuotaLimit(cls, exception=None, provider=None):
        return IngestSpreadsheetError(15200, exception, provider)

    @classmethod
    def SpreadsheetCredentialsError(cls, exception=None, provider=None):
        return IngestSpreadsheetError(15300, exception, provider)


class SpreadsheetFeedingService(FeedingService):
    NAME = 'spreadsheet'

    ERRORS = [
        IngestApiError.apiNotFoundError().get_error_description(),
        ParserError.parseFileError().get_error_description(),
        IngestSpreadsheetError.SpreadsheetPermissionError().get_error_description(),
    ]

    label = 'Google Spreadsheet'

    fields = [
        {
            'id': 'service_account', 'type': 'text', 'label': 'Service account',
            'required': True, 'errors': {15300: 'Invalid service account key'},
        },
        {
            'id': 'url', 'type': 'text', 'label': 'Source',
            'placeholder': 'Google Spreadsheet URL', 'required': True,
            'errors': {
                1001: 'Can\'t parse spreadsheets.',
                1002: 'Can\'t parse spreadsheets.',
                4006: 'URL not found.',
                15100: 'Missing write permission while processing file',
                15200: 'Server reaches read quota limits.'
            }
        }
    ]

    titles = [
        'Start date', 'Start time', 'End date', 'End time', 'All day', 'Timezone', 'Slugline', 'Event name',
        'Description', 'Occurence status', 'Calendars', 'Location Name', 'Location Address', 'Location City/Town',
        'Location State/Province/Region', 'Location Country', 'Contact Honorific', 'Contact First name',
        'Contact Last name', 'Contact Organisation', 'Contact Point of Contact', 'Contact Email',
        'Contact Phone Number', 'Contact Phone Usage', 'Contact Phone Public', 'Long description', 'Internal note',
        'Ed note', 'External links',
    ]

    required_field = [
        'slugline', 'calendars', 'name',
    ]

    required_contact_field = ['Contact Email', 'Contact Phone Number']
    required_location_field = ['Location Name', 'Location Address', 'Location Country']

    occur_status_qcode_mapping = {
        'Unplanned event': 'eocstat:eos0',
        'Planned, occurrence planned only': 'eocstat:eos1',
        'Planned, occurrence highly uncertain': 'eocstat:eos2',
        'Planned, May occur': 'eocstat:eos3',
        'Planned, occurrence highly likely': 'eocstat:eos4',
        'Planned, occurs certainly': 'eocstat:eos5',
    }

    def _test(self, provider):
        return self._update(provider, update=None, test=True)

    def _update(self, provider, update, test=False):
        """Load items from google spreadsheet and insert (update) to events database

        If STATUS field is empty, create new item
        If STATUS field is UPDATED, update item
        """
        config = provider.get('config', {})
        url = config.get('url', '')
        worksheet = self._get_worksheet(url, config.get('service_account', ''))
        try:
            # Get all values to avoid reaching read limit
            data = worksheet.get_all_values()
            # lookup title columns in case it's not followed order
            index = {}
            titles = [s.lower().strip() for s in data[0]]
            for field in self.titles:
                if field.lower().strip() not in titles:
                    raise ParserError.parseFileError()
                index[field] = titles.index(field.lower().strip())

            # avoid maximum limit cols error
            total_col = worksheet.col_count
            if total_col < len(titles) + 3:
                worksheet.add_cols(len(titles) + 3 - total_col)

            for field in ('_STATUS', '_ERR_MESSAGE', '_GUID'):
                if field.lower() not in titles:
                    titles.append(field.lower())
                    worksheet.update_cell(1, len(titles), field)
                index[field] = titles.index(field.lower())

            items = []
            cells_list = []
            # skip first two title rows
            for row in range(3, len(data) + 1):
                if not row:
                    break
                error_message = None
                values = data[row - 1]
                is_updated = None if len(values) < index['_STATUS'] + 1 else values[index['_STATUS']].strip().upper()
                if len(values) - 1 > index['_GUID'] and values[index['_GUID']]:
                    guid = values[index['_GUID']]
                    # find item to check if it's exists and guid is valid
                    if not superdesk.get_resource_service('events').find_one(guid=guid, req=None):
                        raise KeyError('GUID is not exists')
                else:
                    guid = generate_guid(type=GUID_NEWSML)
                try:
                    # avoid momentsJS throw none timezone value error
                    tzone = values[index['Timezone']] if values[index['Timezone']] != 'none' else str(get_localzone())
                    tz = timezone(tzone)
                    start_datetime = tz.localize(parse(values[index['Start date']] + ' ' + values[index['Start time']]))
                    end_date = values[index['End date']]
                    if values[index['All day']] == 'TRUE':
                        # set end datetime to the end of start date
                        end_datetime = tz.localize(parse(values[index['Start date']])) + timedelta(days=1, seconds=-1)
                    else:
                        end_datetime = tz.localize(parse(end_date + ' ' + values[index['End time']]))
                    item = {
                        'type': 'event',
                        'name': values[index['Event name']],
                        'slugline': values[index['Slugline']],
                        'dates': {
                            'start': start_datetime,
                            'end': end_datetime,
                            'tz': tzone,
                        },
                        'occur_status': {
                            'qcode': self.occur_status_qcode_mapping[values[index['Occurence status']]],
                            'name': values[index['Occurence status']],
                            'label': values[index['Occurence status']].lower(),
                        },
                        'definition_short': values[index['Description']],
                        'definition_long': values[index['Long description']],
                        'internal_note': values[index['Internal note']],
                        'ednote': values[index['Ed note']],
                        'links': [values[index['External links']]],
                        'guid': guid,
                        'status': is_updated,
                        'version': 1,
                    }
                    calendars = values[index['Calendars']]
                    if calendars:
                        item['calendars'] = [{
                            'is_active': True,
                            'name': calendars,
                            'qcode': calendars.lower(),
                        }]

                    if all(values[index[field]] for field in self.required_location_field):
                        item['location'] = [{
                            'name': values[index['Location Name']],
                            'address': {
                                'line': [values[index['Location Address']]],
                                'locality': values[index['Location City/Town']],
                                'area': values[index['Location State/Province/Region']],
                                'country': values[index['Location Country']],
                            }
                        }]
                    if all(values[index[field]] for field in self.required_contact_field) \
                        and (
                            all(values[index[field]] for field in ['Contact First name', 'Contact Last name'])
                            or values[index['Contact Organisation']]):
                        is_public = values[index['Contact Phone Public']] == 'TRUE'
                        if values[index['Contact Phone Usage']] == 'Confidential':
                            is_public = False
                        item['contact'] = {
                            'honorific': values[index['Contact Honorific']],
                            'first_name': values[index['Contact First name']],
                            'last_name': values[index['Contact Last name']],
                            'organisation': values[index['Contact Organisation']],
                            'contact_email': [values[index['Contact Email']]],
                            'contact_phone': [{
                                'number': values[index['Contact Phone Number']],
                                'public': is_public,
                                'usage': values[index['Contact Phone Usage']],
                            }]
                        }
                    item.setdefault(ITEM_STATE, CONTENT_STATE.DRAFT)
                    # ignore invalid item
                    missing_fields = [field for field in self.required_field if not item.get(field)]
                    if missing_fields:
                        missing_fields = ', '.join(missing_fields)
                        logger.error(
                            'Provider %s: Ignore event "%s". Missing %s fields',
                            provider.get('name'), item.get('name'), missing_fields,
                        )
                        error_message = 'Missing ' + missing_fields + ' fields'
                    elif not is_updated or is_updated in ('UPDATED', 'ERROR'):
                        cells_list.extend([
                            Cell(row, index['_STATUS'] + 1, 'DONE'),
                            Cell(row, index['_ERR_MESSAGE'] + 1, ''),
                            Cell(row, index['_GUID'] + 1, guid)
                        ])
                        items.append(item)
                except UnknownTimeZoneError:
                    error_message = 'Invalid timezone'
                    logger.error(
                        'Provider %s: Event "%s": Invalid timezone %s',
                        provider.get('name'), values[index['Event name']], tzone
                    )
                except (TypeError, ValueError, KeyError) as e:
                    error_message = e.args[0]
                    logger.error(error_message)

                if error_message:
                    cells_list.extend([
                        Cell(row, index['_STATUS'] + 1, 'ERROR'),
                        Cell(row, index['_ERR_MESSAGE'] + 1, error_message)
                    ])
            if cells_list:
                worksheet.update_cells(cells_list)
            return [items]
        except gspread.exceptions.CellNotFound as e:
            raise ParserError.parseFileError(e)

    def _get_worksheet(self, url, service_account):
        """Get worksheet from google spreadsheet

        :return: worksheet
        :rtype: object

        :raises IngestSpreadsheetError
        """
        scope = [
            'https://spreadsheets.google.com/feeds',
            'https://www.googleapis.com/auth/drive',
        ]

        try:
            service_account = json.loads(service_account)
            credentials = ServiceAccountCredentials.from_json_keyfile_dict(service_account, scope)
            gc = gspread.authorize(credentials)
            spreadsheet = gc.open_by_url(url)
            permission = spreadsheet.list_permissions()[0]
            if permission['role'] != 'writer':
                raise IngestSpreadsheetError.SpreadsheetPermissionError()
            worksheet = spreadsheet.worksheet('Agenda for ingest')
            return worksheet
        except (json.decoder.JSONDecodeError, ValueError):
            raise IngestSpreadsheetError.SpreadsheetCredentialsError()
        except gspread.exceptions.NoValidUrlKeyFound:
            raise IngestApiError.apiNotFoundError()
        except gspread.exceptions.WorksheetNotFound:
            raise ParserError.parseFileError()
        except gspread.exceptions.APIError as e:
            response_code = e.response.json()['error']['code']
            if response_code == 403:
                raise IngestSpreadsheetError.SpreadsheetPermissionError()
            elif response_code == 429:
                raise IngestSpreadsheetError.SpreadsheetQuotaLimit()
            else:
                raise IngestApiError.apiNotFoundError()


register_feeding_service(SpreadsheetFeedingService)
