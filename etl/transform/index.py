import base64
import itertools
import json
import logging
from functools import partial
from multiprocessing.pool import Pool

from etl.common.brapi import get_urn, get_identifier, get_uri, get_entity_links
from etl.common.utils import split_every, remove_empty, remove_none, as_list, first


def load_entity_lines(options):
    entity_name, file_path = options
    with open(file_path, 'r') as json_data_file:
        for line in json_data_file:
            yield (entity_name, line)


def uri_encode(uri):
    if uri:
        return base64.b64encode(uri.encode()).decode()


def parse_data(source, options):
    entity_name, line = options
    data = json.loads(line)

    identifier = get_identifier(entity_name, data)
    urn = get_urn(source, entity_name, identifier)
    uri = get_uri(source, entity_name, data)
    global_identifier = uri_encode(uri)

    # TODO: remove unnecessary fields
    # Add JSON-LD/schema.org basic identification
    data['@id'] = uri
    data['@type'] = entity_name
    data['schema:includedInDataCatalog'] = source['@id']

    # Replace BrAPI identifiers
    data[entity_name + 'DbId'] = global_identifier
    data[entity_name + 'PUI'] = uri
    data['source'] = source['@id']

    id_mapping = {(entity_name, identifier): global_identifier}
    if urn != uri:
        id_mapping[urn] = uri
    return remove_empty(data), id_mapping


def debug_checkpoint(logger, elements, message="", every=1000):
    """
    Intercept iterator to print a checkpoint in the debug logger
    """
    if logger.isEnabledFor(logging.DEBUG):
        n = 0
        for element in elements:
            n += 1
            if n > 0 and n % every == 0:
                logger.debug("checkpoint: {} {}".format(n, message))
            yield element
    else:
        return elements


def extract_id_mapping(data_it, global_id_mapping):
    for data, id_mapping in data_it:
        global_id_mapping.update(id_mapping)
        yield data


def generate_global_id_links(source, global_id_mapping, data):
    def get_global_identifier(entity_name, identifier):
        return global_id_mapping[(entity_name, identifier)]

    entity_id_links = get_entity_links(data, 'DbId')
    for (linked_entity, linked_id_field, plural, linked_ids) in entity_id_links:
        link_uri_field = linked_entity + 'PUI' + plural
        if link_uri_field in data:
            continue
        linked_uris = set(remove_none(
            map(partial(get_global_identifier, linked_entity), as_list(linked_ids))))
        if linked_uris:
            if not plural:
                linked_uris = first(linked_uris)
            data[link_uri_field] = linked_uris

    entity_uri_links = get_entity_links(data, 'PUI')
    for (linked_entity, linked_uri_field, plural, linked_uris) in entity_uri_links:
        linked_id_field = linked_entity + 'DbId' + plural
        linked_ids = set(map(uri_encode, as_list(linked_uris)))
        if linked_ids:
            if not plural:
                linked_ids = first(linked_ids)
            data[linked_id_field] = linked_ids

    return data

def load_index(logger, source, data_index, entity_files):
    """
    Load entity JSON files and index by URN on disk
    """
    # Read JSON files as a stream of lines
    lines = itertools.chain.from_iterable(map(load_entity_lines, entity_files))

    # Parse JSON data
    data = Pool(1).imap_unordered(partial(parse_data, source), lines, 1000)

    # Extract identifier mapping from data stream
    global_id_mapping = {}
    data = extract_id_mapping(data, global_id_mapping)

    # Debug: Intercept object to print checkpoints
    data = debug_checkpoint(logger, data, "object parsed", 5000)

    # Write data in batches into an index by URN
    data_batches = split_every(10000, data)
    for batch in data_batches:
        data_index.begin()
        for data in batch:
            data_index[data['URN']] = data
        data_index.commit()

    # Replace ids in links
    data_batches = split_every(10000, data_index.values())
    for batch in data_batches:
        data_index.begin()
        for data in batch:
            entity_id_links = get_entity_links(data, 'DbId')
            for (linked_entity, linked_id_field, plural, linked_ids) in entity_id_links:
                link_uri_field = linked_entity + 'PUI' + plural
                if link_uri_field in data:
                    continue
                linked_uris = set(remove_none(
                    map(partial(get_or_generate_uri, linked_entity), as_list(linked_ids))))
                if linked_uris:
                    if not plural:
                        linked_uris = first(linked_uris)
                    data[link_uri_field] = linked_uris

            entity_uri_links = get_entity_links(data, 'PUI')
            for (linked_entity, linked_uri_field, plural, linked_uris) in entity_uri_links:
                linked_id_field = linked_entity + 'DbId' + plural
                linked_ids = set(map(uri_encode, as_list(linked_uris)))
                if linked_ids:
                    if not plural:
                        linked_ids = first(linked_ids)
                    data[linked_id_field] = linked_ids

        data_index.commit()
    return data_index
