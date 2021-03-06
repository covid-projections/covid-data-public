import dataclasses
import datetime
import enum
from typing import Tuple
from typing import Union, Optional
import os
import pathlib

import click
import pandas as pd

import structlog
import pydantic
from structlog._config import BoundLoggerLazyProxy

from covidactnow.datapublic import common_init
from covidactnow.datapublic import common_df
from covidactnow.datapublic.common_fields import (
    GetByValueMixin,
    CommonFields,
    COMMON_FIELDS_TIMESERIES_KEYS,
    FieldNameAndCommonField,
)
from scripts import helpers
import covidcountydata
from scripts import update_nytimes_data

DATA_ROOT = pathlib.Path(__file__).parent.parent / "data"


class StaleDataError(Exception):
    pass


# Keep in sync with COMMON_FIELD_MAP in covid_county_data.py in the covid-data-model repo.
# Fields commented out with tag 20200616 were not found in the data used by update_covid_county_data_test.py.
@enum.unique
class Fields(GetByValueMixin, FieldNameAndCommonField, enum.Enum):
    LOCATION = "location", None  # Special transformation to FIPS
    DT = "dt", CommonFields.DATE

    NEGATIVE_TESTS_TOTAL = "negative_tests_total", CommonFields.NEGATIVE_TESTS
    POSITIVE_TESTS_TOTAL = "positive_tests_total", CommonFields.POSITIVE_TESTS
    TESTS_TOTAL = "tests_total", CommonFields.TOTAL_TESTS

    ACTIVE_TOTAL = "active_total", None
    CASES_TOTAL = "cases_total", CommonFields.CASES
    CASES_CONFIRMED = "cases_confirmed", None
    CASES_SUSPECTED = "cases_suspected", None
    RECOVERED_TOTAL = "recovered_total", CommonFields.RECOVERED
    DEATHS_TOTAL = "deaths_total", CommonFields.DEATHS
    DEATHS_CONFIRMED = "deaths_confirmed", None
    DEATHS_SUSPECTED = "deaths_suspected", None

    HOSPITAL_BEDS_CAPACITY_COUNT = "hospital_beds_capacity_count", CommonFields.STAFFED_BEDS
    HOSPITAL_BEDS_IN_USE_COVID_CONFIRMED = "hospital_beds_in_use_covid_confirmed", None
    HOSPITAL_BEDS_IN_USE_COVID_NEW = "hospital_beds_in_use_covid_new", None
    HOSPITAL_BEDS_IN_USE_COVID_SUSPECTED = "hospital_beds_in_use_covid_suspected", None
    HOSPITAL_BEDS_IN_USE_ANY = "hospital_beds_in_use_any", CommonFields.HOSPITAL_BEDS_IN_USE_ANY
    HOSPITAL_BEDS_IN_USE_COVID_TOTAL = (
        "hospital_beds_in_use_covid_total",
        CommonFields.CURRENT_HOSPITALIZED,
    )
    NUM_HOSPITALS_REPORTING = "num_hospitals_reporting", None
    NUM_OF_HOSPITALS = "num_of_hospitals", None

    ICU_BEDS_CAPACITY_COUNT = "icu_beds_capacity_count", CommonFields.ICU_BEDS
    ICU_BEDS_IN_USE_COVID_CONFIRMED = "icu_beds_in_use_covid_confirmed", None
    ICU_BEDS_IN_USE_COVID_SUSPECTED = "icu_beds_in_use_covid_suspected", None
    ICU_BEDS_IN_USE_ANY = "icu_beds_in_use_any", CommonFields.CURRENT_ICU_TOTAL
    ICU_BEDS_IN_USE_COVID_TOTAL = ("icu_beds_in_use_covid_total", CommonFields.CURRENT_ICU)

    VENTILATORS_IN_USE_ANY = "ventilators_in_use_any", None
    VENTILATORS_CAPACITY_COUNT = "ventilators_capacity_count", None
    VENTILATORS_IN_USE_COVID_TOTAL = (
        "ventilators_in_use_covid_total",
        CommonFields.CURRENT_VENTILATED,
    )
    VENTILATORS_IN_USE_COVID_CONFIRMED = "ventilators_in_use_covid_confirmed", None
    VENTILATORS_IN_USE_COVID_SUSPECTED = "ventilators_in_use_covid_suspected", None


# Keep in sync with COMMON_FIELD_MAP in covid-data-model repo.
@enum.unique
class UsaFactsFields(GetByValueMixin, FieldNameAndCommonField, enum.Enum):
    FIPS = "fips", None  # Special transformation
    DT = "dt", CommonFields.DATE

    CASES_TOTAL = "cases_total", CommonFields.CASES
    DEATHS_TOTAL = "deaths_total", CommonFields.DEATHS


@dataclasses.dataclass
class CovidCountyDataTransformer:
    """Get the newest data from Valorum / Covid Modeling Data Collaborative and return a DataFrame
    of timeseries."""

    # API key, see https://github.com/valorumdata/covid_county_data.py#api-keys
    covid_county_data_key: Optional[str]

    # Path of a text file of state names, copied from census.gov
    census_state_path: pathlib.Path

    # FIPS for each county, by name
    county_fips_csv: pathlib.Path

    log: Union[structlog.BoundLoggerBase, BoundLoggerLazyProxy]

    @staticmethod
    def make_with_data_root(
        data_root: pathlib.Path,
        covid_county_data_key: Optional[str],
        log: Union[structlog.BoundLoggerBase, BoundLoggerLazyProxy],
    ) -> "CovidCountyDataTransformer":
        return CovidCountyDataTransformer(
            covid_county_data_key=covid_county_data_key,
            census_state_path=data_root / "misc" / "state.txt",
            county_fips_csv=data_root / "misc" / "fips_population.csv",
            log=log,
        )

    def transform(self) -> pd.DataFrame:
        client = covidcountydata.Client(apikey=self.covid_county_data_key)

        client.covid_us()
        df = client.fetch()

        _fail_if_no_recent_dates(df[Fields.DT])

        df[CommonFields.FIPS] = helpers.fips_from_int(df[Fields.LOCATION])

        # Already transformed from Fields to CommonFields
        already_transformed_fields = {CommonFields.FIPS}

        df = helpers.rename_fields(df, Fields, already_transformed_fields, self.log)

        counties, states = self._counties_states_with_geoattributes(df)

        # TX county data is shifted forward one day.
        # it's possible that more regions are also shifted, see
        # https://trello.com/c/wvH5sgfi/404-valorum-tx-data-shifted-forward-one-day
        # for more information.
        counties = counties.set_index(CommonFields.DATE)
        is_tx_counties = counties[CommonFields.FIPS].str.startswith("48")
        tx_counties = counties.loc[is_tx_counties]
        tx_counties = tx_counties.shift(-1)
        # Drop the column at the end with all nulls.
        tx_counties = tx_counties.dropna(how="all")
        counties = pd.concat([counties.loc[~is_tx_counties], tx_counties]).reset_index()

        # Hacky way of re-using nytimes code to remove county backfills.
        # TODO(chris): make code more generic. May be that the code belongs further
        # downstream in the data pipeline.
        backfilled_cases = update_nytimes_data.COUNTY_BACKFILLED_CASES
        counties = update_nytimes_data.remove_county_backfilled_cases(counties, backfilled_cases)

        # State level bed data is coming from HHS which tend to not match
        # numbers we're seeing from Covid Care Map.
        state_columns_to_drop = [
            CommonFields.ICU_BEDS,
            CommonFields.HOSPITAL_BEDS_IN_USE_ANY,
            CommonFields.STAFFED_BEDS,
            CommonFields.CURRENT_ICU_TOTAL,
        ]
        states = states.drop(state_columns_to_drop, axis="columns")

        df = pd.concat([states, counties])
        df = common_df.sort_common_field_columns(df)
        df = self._drop_bad_rows(df)

        # Removing a string of misleading FL current_icu values.
        is_incorrect_fl_icu_dates = df[CommonFields.DATE].between("2020-05-14", "2020-05-20")
        is_fl_state = df[CommonFields.FIPS] == "12"
        df.loc[is_fl_state & is_incorrect_fl_icu_dates, CommonFields.CURRENT_ICU] = None

        df = df.set_index(COMMON_FIELDS_TIMESERIES_KEYS, verify_integrity=True)

        return df

    def transform_usafacts(self) -> pd.DataFrame:
        client = covidcountydata.Client(apikey=self.covid_county_data_key)

        client.usafacts_covid()
        df = client.fetch()

        _fail_if_no_recent_dates(df[UsaFactsFields.DT], stale_days_allowed=7)

        df[CommonFields.FIPS] = helpers.fips_from_int(df[UsaFactsFields.FIPS])

        # Already transformed from Fields to CommonFields
        already_transformed_fields = {CommonFields.FIPS}

        df = helpers.rename_fields(df, UsaFactsFields, already_transformed_fields, self.log)

        counties, states = self._counties_states_with_geoattributes(df)

        df = pd.concat([states, counties])
        df = common_df.sort_common_field_columns(df)
        df = self._drop_bad_rows(df)

        df = df.set_index(COMMON_FIELDS_TIMESERIES_KEYS, verify_integrity=True)

        return df

    def _drop_bad_rows(self, df):
        bad_rows = (
            df[CommonFields.FIPS].isnull()
            | df[CommonFields.DATE].isnull()
            | df[CommonFields.STATE].isnull()
        )
        if bad_rows.any():
            self.log.warning(
                "Dropping rows with null in important columns", bad_rows=str(df.loc[bad_rows])
            )
            df = df.loc[~bad_rows]
        # Work around for https://github.com/valorumdata/cmdc-tools/issues/131
        ancient_rows = df[CommonFields.DATE] < "2019-12-01"
        if ancient_rows.any():
            self.log.info("Dropping rows of ancient data", bad_rows=str(df.loc[ancient_rows]))
            df = df.loc[~ancient_rows]
        return df

    def _counties_states_with_geoattributes(
        self, df: pd.DataFrame
    ) -> Tuple[pd.DataFrame, pd.DataFrame]:
        """Split a DataFrame into using FIPS length, then join with geo attributes loaded from other data files.

        Returns:
            counties and states DataFrame objects
        """
        df[CommonFields.COUNTRY] = "USA"
        # Partition df by region type so states and counties can by merged with different
        # data to get their names.
        state_mask = df[CommonFields.FIPS].str.len() == 2
        states = df.loc[state_mask, :]
        counties = df.loc[~state_mask, :]
        fips_data = helpers.load_county_fips_data(self.county_fips_csv).set_index(
            [CommonFields.FIPS]
        )
        counties = counties.merge(
            fips_data[[CommonFields.STATE, CommonFields.COUNTY]],
            left_on=[CommonFields.FIPS],
            suffixes=(False, False),
            how="left",
            right_index=True,
        )
        no_match_counties_mask = counties.state.isna()
        if no_match_counties_mask.sum() > 0:
            self.log.warning(
                "Some counties did not match by fips",
                bad_fips=counties.loc[no_match_counties_mask, CommonFields.FIPS].unique().tolist(),
            )
        counties = counties.loc[~no_match_counties_mask, :]
        counties[CommonFields.AGGREGATE_LEVEL] = "county"

        state_df = helpers.load_census_state(self.census_state_path).set_index(CommonFields.FIPS)
        states = states.merge(
            state_df[[CommonFields.STATE]],
            left_on=[CommonFields.FIPS],
            suffixes=(False, False),
            how="left",
            right_index=True,
        )
        states[CommonFields.AGGREGATE_LEVEL] = "state"

        return counties, states


def _fail_if_no_recent_dates(dates: pd.Series, stale_days_allowed=3):
    """Raise an execption if there are no recent dates in Series"""
    latest_dt = dates.max()
    if latest_dt < datetime.date.today() - datetime.timedelta(days=stale_days_allowed):
        raise StaleDataError(f"Latest dt is {latest_dt}")


@click.command()
@click.option("--fetch-covid-us/--no-fetch-covid-us", default=True)
@click.option("--fetch-usafacts-covid/--no-fetch-usafacts-covid", default=True)
def main(fetch_covid_us: bool, fetch_usafacts_covid: bool):
    common_init.configure_logging()
    log = structlog.get_logger()
    transformer = CovidCountyDataTransformer.make_with_data_root(
        DATA_ROOT, os.environ.get("CMDC_API_KEY", None), log
    )

    if fetch_covid_us:
        common_df.write_csv(
            common_df.only_common_columns(transformer.transform(), log),
            DATA_ROOT / "cases-covid-county-data" / "timeseries-common.csv",
            log,
        )

    if fetch_usafacts_covid:
        common_df.write_csv(
            common_df.only_common_columns(transformer.transform_usafacts(), log),
            DATA_ROOT / "cases-covid-county-data" / "timeseries-usafacts.csv",
            log,
        )


if __name__ == "__main__":
    main()  # pylint: disable=no-value-for-parameter
