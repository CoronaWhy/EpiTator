#!/usr/bin/env python
from __future__ import absolute_import
from .annotator import Annotator, AnnoTier
from .annospan import AnnoSpan, SpanGroup
from .structured_data_annotator import StructuredDataAnnotator
from .geoname_annotator import GeonameAnnotator
from .resolved_keyword_annotator import ResolvedKeywordAnnotator
from .spacy_annotator import SpacyAnnotator
from .date_annotator import DateAnnotator
from .raw_number_annotator import RawNumberAnnotator
import re
import datetime
import logging
logging.basicConfig(level=logging.ERROR, format='%(asctime)s %(message)s')
logger = logging.getLogger(__name__)


class Table():
    def __init__(self, column_definitions, rows, metadata=None):
        self.column_definitions = column_definitions
        self.rows = rows
        self.metadata = metadata or {}


def is_null(val_string):
    val_string = val_string.strip()
    return val_string == "" or val_string == "-"


def median(li):
    mid_idx = (len(li) - 1) / 2
    li = sorted(li)
    if len(li) % 2 == 1:
        return li[mid_idx]
    else:
        return (li[mid_idx] + li[mid_idx + 1]) / 2


def merge_metadata(sofar, child_metadata):
    # prefer highest weighted species
    if "species" in sofar and "species" in child_metadata:
        if sofar['species']['weight'] < child_metadata['species']['weight']:
            return dict(child_metadata, **dict(sofar, species=child_metadata['species']))
    return dict(child_metadata, **sofar)


def combine_metadata(spans):
    """
    Return the merged metadata dictionaries from all descendant spans.
    Presedence of matching properties follows the order of a pre-order tree traversal.
    """
    result = {}
    for span in spans:
        child_metadata = combine_metadata(span.base_spans)
        if span.metadata:
            child_metadata = merge_metadata(span.metadata, child_metadata)
        result = merge_metadata(result, child_metadata)
    return result


def split_list(li):
    group = []
    for value in li:
        if value:
            group.append(value)
        else:
            if len(group) > 0:
                yield group
                group = []
    if len(group) > 0:
        yield group


class StructuredIncidentAnnotator(Annotator):
    """
    The structured incident annotator will find groupings of case counts and incidents
    """

    def annotate(self, doc):
        if 'structured_data' not in doc.tiers:
            doc.add_tiers(StructuredDataAnnotator())
        if 'geonames' not in doc.tiers:
            doc.add_tiers(GeonameAnnotator())
        if 'dates' not in doc.tiers:
            doc.add_tiers(DateAnnotator())
        if 'resolved_keywords' not in doc.tiers:
            doc.add_tiers(ResolvedKeywordAnnotator())
        if 'spacy.tokens' not in doc.tiers:
            doc.add_tiers(SpacyAnnotator())
        if 'raw_numbers' not in doc.tiers:
            doc.add_tiers(RawNumberAnnotator())

        geonames = doc.tiers['geonames']
        dates = doc.tiers['dates']
        resolved_keywords = doc.tiers['resolved_keywords']
        spacy_tokens = doc.tiers['spacy.tokens']
        numbers = doc.tiers['raw_numbers']
        species_list = []
        for k in resolved_keywords:
            for resolution in k.metadata.get('resolutions', []):
                if resolution['entity']['type'] == 'species':
                    species_list.append(AnnoSpan(
                        k.start,
                        k.end,
                        k.doc,
                        metadata={'species': resolution}))
                    break
        entities_by_type = {
            'geoname': geonames,
            'date': dates,
            'species': AnnoTier(species_list).optimal_span_set(),
            'number': numbers,
            'incident_type': spacy_tokens.search_spans(r'(case|death)s?'),
            'incident_status': spacy_tokens.search_spans(r'suspected|confirmed'),
        }
        tables = []
        for span in doc.tiers['structured_data'].spans:
            if span.metadata['type'] != 'table':
                continue
            # Add columns based on surrounding text
            table_title = doc.tiers['spacy.sentences'].span_before(span)
            if table_title:
                table_title = AnnoSpan(
                    table_title.start,
                    min(table_title.end, span.start),
                    doc)
            last_geoname_mentioned = geonames.span_before(span)
            last_date_mentioned = dates.span_before(span)
            rows = span.metadata['data']
            # Detect header
            first_row = AnnoTier(rows[0])
            logger.info("header")
            logger.info(first_row)
            header_entities = list(first_row.group_spans_by_containing_span(numbers))
            if all(len(entity_spans) == 0 for header_span, entity_spans in header_entities):
                has_header = True
            else:
                has_header = False
            if has_header:
                data_rows = rows[1:]
            else:
                data_rows = rows

            # Remove rows without the right number of columns
            median_num_cols = median(map(len, rows))
            data_rows = [row for row in rows if len(row) == median_num_cols]

            # Determine column types
            table_by_column = zip(*data_rows)
            column_types = []
            parsed_column_entities = []
            for column_values in table_by_column:
                num_non_null_rows = sum(not is_null(value.text) for value in column_values)
                column_values = AnnoTier(column_values)
                # Choose column type based on greatest percent match,
                # if under 30, choose text.
                max_matches = 0
                matching_column_entities = None
                column_type = "text"
                for value_type, value_spans in entities_by_type.items():
                    filtered_value_spans = value_spans
                    if value_type == "number":
                        filtered_value_spans = value_spans.without_overlaps(dates)
                    column_entities = [
                        SpanGroup(contained_spans, metadata=combine_metadata(contained_spans)) if len(contained_spans) > 0 else None
                        for group_span, contained_spans in column_values.group_spans_by_containing_span(filtered_value_spans)]
                    num_matches = sum(
                        contained_spans is not None
                        for contained_spans in column_entities)
                    if num_non_null_rows > 0 and float(num_matches) / num_non_null_rows > 0.3:
                        if num_matches > max_matches:
                            max_matches = num_matches
                            matching_column_entities = column_entities
                            column_type = value_type
                    if matching_column_entities is None:
                        matching_column_entities = [[] for x in column_values]
                column_types.append(column_type)
                parsed_column_entities.append(matching_column_entities)
            column_definitions = []
            if has_header:
                for column_type, header_name in zip(column_types, first_row):
                    column_definitions.append({
                        'name': header_name.text,
                        'type': column_type
                    })
            else:
                column_definitions = [
                    {'type': column_type}
                    for column_type in column_types]

            rows = zip(*parsed_column_entities)
            logger.info("%s rows" % len(rows))
            date_period = None
            for column_def, entities in zip(column_definitions, parsed_column_entities):
                if column_def['type'] == 'date':
                    date_diffs = []
                    for entity_group in split_list(entities):
                        date_diffs += [
                            abs(d.metadata['datetime_range'][0] - next_d.metadata['datetime_range'][0])
                            for d, next_d in zip(entity_group, entity_group[1:])]
                    date_period = median(date_diffs)
                    break
            tables.append(Table(
                column_definitions,
                rows,
                metadata=dict(
                    title=table_title,
                    date_period=date_period,
                    aggregation="cumulative" if re.search("cumulative", table_title.text, re.I) else None,
                    last_geoname_mentioned=last_geoname_mentioned,
                    last_date_mentioned=last_date_mentioned)))
        incidents = []
        for table in tables:
            for row_idx, row in enumerate(table.rows):
                row_incident_date = table.metadata.get('last_date_mentioned')
                row_incident_location = table.metadata.get('last_geoname_mentioned')
                row_incident_species = table.metadata.get('last_species_mentioned')
                row_incident_base_type = None
                row_incident_status = None
                row_incident_aggregation = table.metadata.get('aggregation')
                for column, value in zip(table.column_definitions, row):
                    if not value:
                        continue
                    if column['type'] == 'date':
                        row_incident_date = value
                    elif column['type'] == 'geoname':
                        row_incident_location = value
                    elif column['type'] == 'species':
                        row_incident_species = value
                    elif column['type'] == 'incident_type':
                        if "case" in value.text.lower():
                            row_incident_base_type = "caseCount"
                        elif "death" in value.text.lower():
                            row_incident_base_type = "deathCount"
                    elif column['type'] == 'incident_status':
                        row_incident_status = value.text

                row_incidents = []
                for column, value in zip(table.column_definitions, row):
                    if not value:
                        continue
                    if column['type'] == "number":
                        column_name = column.get('name', '').lower()
                        incident_base_type = None
                        if row_incident_base_type:
                            incident_base_type = row_incident_base_type
                        else:
                            if "cases" in column_name:
                                incident_base_type = "caseCount"
                            elif "deaths" in column_name:
                                incident_base_type = "deathCount"
                        if row_incident_status:
                            count_status = row_incident_status
                        else:
                            if "suspect" in column_name or column_name == "reported":
                                count_status = "suspected"
                            elif "confirmed" in column_name:
                                count_status = "confirmed"
                            else:
                                count_status = None
                        if count_status and not incident_base_type:
                            incident_base_type = "caseCount"
                        incident_aggregation = None
                        if row_incident_aggregation is not None:
                            incident_aggregation = row_incident_aggregation
                        elif "total" in column_name:
                            incident_aggregation = "cumulative"
                        elif "new" in column_name:
                            incident_aggregation = "incremental"
                        incident_count = value.metadata['number']
                        incident_location = row_incident_location
                        incident_species = row_incident_species
                        incident_date = row_incident_date
                        if not incident_base_type:
                            continue
                        if incident_species:
                            species_entity = incident_species.metadata['species']['entity']
                            incident_species = {
                                'id': species_entity['id'],
                                'label': species_entity['label'],
                            }
                        if incident_date:
                            incident_date = incident_date.metadata['datetime_range']
                        if table.metadata.get('date_period'):
                            if incident_aggregation != "cumulative":
                                incident_date = [
                                    incident_date[0] - table.metadata.get('date_period'),
                                    incident_date[0]]
                        row_incidents.append(AnnoSpan(value.start, value.end, doc, metadata={
                            'base_type': incident_base_type,
                            'aggregation': incident_aggregation,
                            'value': incident_count,
                            'attributes': filter(lambda x: x, [count_status]),
                            'location': incident_location.metadata['geoname'].to_dict() if incident_location else None,
                            'dateRange': incident_date,
                            'species': incident_species
                        }))
                # If a count is marked as incremental any count in the row above
                # that value is considered cumulative.
                max_new_cases = -1
                max_new_deaths = -1
                for incident_span in row_incidents:
                    incident = incident_span.metadata
                    if incident['aggregation'] == "incremental":
                        if incident['base_type'] == 'caseCount':
                            if max_new_cases < incident['value']:
                                max_new_cases = incident['value']
                        else:
                            if max_new_deaths < incident['value']:
                                max_new_deaths = incident['value']
                for incident_span in row_incidents:
                    incident = incident_span.metadata
                    if incident['aggregation'] is None:
                        if incident['base_type'] == 'caseCount':
                            if max_new_cases >= 0 and incident['value'] > max_new_cases:
                                incident['aggregation'] = 'cumulative'
                        else:
                            if max_new_deaths >= 0 and incident['value'] > max_new_deaths:
                                incident['aggregation'] = 'cumulative'
                for incident_span in row_incidents:
                    incident = incident_span.metadata
                    if incident['aggregation'] == 'cumulative':
                        incident['type'] = "cumulative" + incident['base_type'][0].upper() + incident['base_type'][1:]
                    else:
                        incident['type'] = incident['base_type']
                    del incident['base_type']
                    del incident['aggregation']
                incidents.extend(row_incidents)
        return {'structured_incidents': AnnoTier(incidents)}
