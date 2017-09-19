import decimal
import logging

from boto3.dynamodb.types import TypeDeserializer

deser = TypeDeserializer()


log = logging.getLogger('historical')


def remove_current_specific_fields(obj):
    """Remove all fields that belong to the Current table -- that don't belong in the Durable table"""
    # TTL:
    del obj["ttl"]
    return obj


def replace_decimals(obj):
    """Recursively replace Decimal objects with floats or ints."""
    if isinstance(obj, list):
        for i in range(len(obj)):
            obj[i] = replace_decimals(obj[i])
        return obj
    elif isinstance(obj, dict):
        for k, v in obj.items():
            obj[k] = replace_decimals(v)
        return obj
    elif isinstance(obj, decimal.Decimal):
        if obj % 1 == 0:
            return int(obj)
        else:
            return float(obj)
    else:
        return obj


def replace_nones(obj):
    """Recursively replace Empty strings with empty objects because Pynamo thinks empty strings are actually None"""
    if isinstance(obj, list):
        for i in range(len(obj)):
            obj[i] = replace_nones(obj[i])
        return obj
    elif isinstance(obj, dict):
        for k, v in obj.items():
            obj[k] = replace_nones(v)
        return obj
    elif obj is None:
        return {}   # Pynamo doesn't like empty strings...
    else:
        return obj


def modify_record(durable_model, current_revision, arn, event_time, diff_func):
    """Handles a DynamoDB MODIFY event type."""
    # We want the newest items first.
    # See: http://docs.aws.amazon.com/amazondynamodb/latest/developerguide/Query.html
    items = list(durable_model.query(arn, eventTime__le=event_time, scan_index_forward=False, limit=1))
    if items:
        latest_revision = items[0]

        # Determine if there is truly a difference, disregarding Ephemeral Paths
        if diff_func(latest_revision, current_revision):
            current_revision.save()
            log.debug('Difference found saving new revision to durable table.')
    else:
        log.warning('Got modify event but no current revision found. Arn: {arn}'.format(arn=arn))


def delete_record(old_image, durable_model):
    """Handles a DynamoDB DELETE event type."""
    data = {}
    for item in old_image:
        data[item] = deser.deserialize(old_image[item])

    data['configuration'] = {}

    # we give our own timestamps for TTL deletions
    del data['eventTime']
    durable_model(**data).save()
    log.debug('Adding deletion marker.')


def process_dynamodb_record(record, durable_model, diff_func):
    """Processes a group of DynamoDB NewImage records."""
    log.info('Processing stream record...')

    arn = record['dynamodb']['Keys']['arn']['S']

    if record['eventName'] in ['INSERT', 'MODIFY']:
        new = remove_current_specific_fields(record['dynamodb']['NewImage'])
        data = {}

        for item in new:
            # this could end up as loss of precision
            data[item] = replace_decimals(deser.deserialize(new[item]))

        current_revision = durable_model(**data)
        if record['eventName'] == 'INSERT':
            current_revision.save()
            log.debug('Saving new revision to durable table.')

        elif record['eventName'] == 'MODIFY':
            modify_record(durable_model, current_revision, arn, data['eventTime'], diff_func)

    if record['eventName'] == 'REMOVE':
        # only track deletes that are from the dynamodb TTL service
        if record.get('userIdentity'):
            if record['userIdentity']['type'] == 'Service':
                if record['userIdentity']['principalId'] == 'dynamodb.amazonaws.com':
                    old_image = remove_current_specific_fields(record['dynamodb']['OldImage'])
                    delete_record(old_image, durable_model)
