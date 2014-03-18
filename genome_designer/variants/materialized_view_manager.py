"""
Manages the Materialized view of the Variant data for filtering.
"""

from django.db import connection
from django.db import transaction

from melted_variant_schema import *


class AbstractMaterializedViewManager(object):
    """Base class for object acting as wrapper for a Postgresql materialized
    view (available starting Postgresql 9.3)
    """

    def __init(self):
        """Child classes should implement at least these two fields.
        """
        # Name of the table in Postgres.
        self.view_table_name = None

        # Database cursor.
        self.cursor = None

    def get_table_name(self):
        """Get the name of the underlying SQL table.
        """
        raise NotImplementedError("Child classes must implement.")

    def create(self):
        """Creates the materialized view in the Postgresql DB.
        """
        # Drop the existing table.
        self.drop()

        # Delegate to child class.
        self.create_internal()

    def create_internal(self):
        """Creates the materialized view in the Postgresql DB.

        Child classes should implement.
        """
        raise NotImplementedError("Child classes must implement.")

    def refresh(self):
        """Refreshes the view.
        """
        assert self.view_table_name
        assert self.cursor
        refresh_statement = 'REFRESH MATERIALIZED VIEW %s ' % (
                self.view_table_name)
        self.cursor.execute(refresh_statement)

    def drop(self):
        """Drops the materialized view in the Postgresql DB.
        """
        assert self.view_table_name
        assert self.cursor
        drop_sql_statement = "DROP MATERIALIZED VIEW IF EXISTS %s" % (
                self.view_table_name,)
        self.cursor.execute(drop_sql_statement)
        transaction.commit_unless_managed()

    def create_if_not_exists_or_invalid(self):
        """Creates the table if it doesn't exist or is not valid.

        NOTE: There is also a refresh() method, which would prevent having
        to create the materialized view. As of this note, we are not using
        refresh() anywhere.  It's not clear whether a refresh is any faster
        than just dropping the table and create it again.
        """
        if not self.check_table_exists() or not self.is_valid():
            self.create()

    def is_valid(self):
        """Indicates whether the table needs to be refreshed.

        Abstract implementation conservative.
        """
        return False

    def check_table_exists(self):
        """Check if the table exists.

        NOTE: Figured out the raw sql query by running psql with -E flag
        and then calling \d. The -E flag causes the raw sql of the commands
        to be shown.
        """
        assert self.view_table_name
        assert self.cursor
        raw_sql = (
            'SELECT c.relname '
            'FROM pg_catalog.pg_class c '
            'WHERE c.relkind=%s AND c.relname=%s '
        )
        self.cursor.execute(raw_sql, ('m', self.view_table_name))
        return bool(self.cursor.fetchone())


class MeltedVariantMaterializedViewManager(AbstractMaterializedViewManager):
    """Interface for objects providing a wrapper for a Postgresql materialized
    view.
    """

    def __init__(self, reference_genome):
        self.reference_genome = reference_genome
        self.view_table_name = self.get_table_name()
        self.cursor = connection.cursor()

    def get_table_name(self):
        """Override.
        """
        return 'materialized_melted_variant_' + self.reference_genome.uid

    def is_valid(self):
        """Override.
        """
        return self.reference_genome.is_materialized_variant_view_valid

    def create_internal(self):
        """Override.
        """
        # Query all columsn except the catch-all key value fields first,
        # then join with the key-value columns.
        create_sql_statement = (
            'CREATE MATERIALIZED VIEW %s AS ('
                'WITH melted_variant_data AS ('
                    '('
                        'SELECT %s FROM main_variant '
                            'INNER JOIN main_variantcallercommondata ON (main_variant.id = main_variantcallercommondata.variant_id) '
                            'INNER JOIN main_variantevidence ON (main_variantcallercommondata.id = main_variantevidence.variant_caller_common_data_id) '
                            'INNER JOIN main_experimentsample ON (main_variantevidence.experiment_sample_id = main_experimentsample.id) '

                            # VariantSet
                            'LEFT JOIN main_varianttovariantset_sample_variant_set_association ON ('
                                    'main_experimentsample.id = main_varianttovariantset_sample_variant_set_association.experimentsample_id) '
                            'LEFT JOIN main_varianttovariantset ON ('
                                    'main_varianttovariantset_sample_variant_set_association.varianttovariantset_id = main_varianttovariantset.id AND '
                                    'main_varianttovariantset.variant_id = main_variant.id) '
                            'LEFT JOIN main_variantset ON main_varianttovariantset.variant_set_id = main_variantset.id '

                            # VariantAlternate
                            'LEFT JOIN main_variantevidence_variantalternate_set ON ('
                                    'main_variantevidence.id = main_variantevidence_variantalternate_set.variantevidence_id) '
                            'LEFT JOIN main_variantalternate ON main_variantevidence_variantalternate_set.variantalternate_id = main_variantalternate.id '
                        'WHERE (main_variant.reference_genome_id = %d) '
                        'GROUP BY %s'
                    ') '
                    'UNION '
                    '('
                        'SELECT %s FROM main_variant '
                            'INNER JOIN main_variantalternate ON main_variantalternate.variant_id = main_variant.id '
                            'INNER JOIN main_varianttovariantset ON main_variant.id = main_varianttovariantset.variant_id '
                            'INNER JOIN main_variantset ON main_varianttovariantset.variant_set_id = main_variantset.id '
                        'WHERE (main_variant.reference_genome_id = %d) '
                        'GROUP BY %s'
                    ') '
                    'ORDER BY position, experiment_sample_uid DESC '
                ') ' # melted_variant_data
                ', va_data_table AS ('
                    'SELECT id, data AS va_data from main_variantalternate'
                ') ' # va_data_table
                ', vccd_data_table AS ('
                    'SELECT id, data AS vccd_data from main_variantcallercommondata'
                ') ' # vccd_data_table
                ', ve_data_table AS ('
                    'SELECT id, data AS ve_data from main_variantevidence'
                ') ' # ve_data_table
                'SELECT melted_variant_data.*, va_data, vccd_data, ve_data '
                    'FROM melted_variant_data '
                        'LEFT JOIN va_data_table ON va_data_table.id = melted_variant_data.va_id '
                        'LEFT JOIN ve_data_table ON ve_data_table.id = melted_variant_data.ve_id '
                        'LEFT JOIN vccd_data_table ON vccd_data_table.id = melted_variant_data.vccd_id'
            ')'
            % (
                    self.view_table_name,

                    MATERIALIZED_TABLE_SELECT_CLAUSE,
                    self.reference_genome.id,
                    MATERIALIZED_TABLE_GROUP_BY_CLAUSE,

                    MATERIALIZED_TABLE_VTVS_SELECT_CLAUSE,
                    self.reference_genome.id,
                    MATERIALIZED_TABLE_VTVS_GROUP_BY_CLAUSE)
            )
        self.cursor.execute(create_sql_statement)
        transaction.commit_unless_managed()

        # Set the valid bit.
        self.reference_genome.is_materialized_variant_view_valid = True
        self.reference_genome.save()
