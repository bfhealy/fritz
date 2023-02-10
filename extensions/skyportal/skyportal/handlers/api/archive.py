import numpy as np
import pandas as pd
from penquins import Kowalski
import uuid
from sqlalchemy.exc import IntegrityError

from baselayer.log import make_log
from baselayer.app.access import auth_or_token, permissions
from baselayer.app.env import load_env
from ..base import BaseHandler
from ...models import Instrument, Source, Stream
from skyportal.model_util import create_token, delete_token
from .photometry import add_external_photometry
from .source import post_source
from ...models import (
    Obj,
    Group,
    Annotation,
)

env, cfg = load_env()
log = make_log("archive")


# A (dedicated) Kowalski instance holding the ZTF light curve data referred to as Gloria
try:
    gloria = Kowalski(
        token=cfg["app.gloria.token"],
        protocol=cfg["app.gloria.protocol"],
        host=cfg["app.gloria.host"],
        port=int(cfg["app.gloria.port"]),
        timeout=10,
    )
    connection_ok = gloria.ping()
    log(f"Gloria connection OK: {connection_ok}")
    if not connection_ok:
        gloria = None
except Exception as e:
    log(f"Gloria connection failed: {str(e)}")
    gloria = None


def radec_to_iau_name(ra: float, dec: float, prefix: str = "ZTFJ"):
    """Transform R.A./Decl. in degrees to IAU-style hexadecimal designations."""
    if not 0.0 <= ra < 360.0:
        raise ValueError("Bad RA value in degrees")
    if not -90.0 <= dec <= 90.0:
        raise ValueError("Bad Dec value in degrees")

    ra_h = np.floor(ra * 12.0 / 180.0)
    ra_m = np.floor((ra * 12.0 / 180.0 - ra_h) * 60.0)
    ra_s = ((ra * 12.0 / 180.0 - ra_h) * 60.0 - ra_m) * 60.0

    dec_d = np.floor(abs(dec)) * np.sign(dec)
    dec_m = np.floor(np.abs(dec - dec_d) * 60.0)
    dec_s = np.abs(np.abs(dec - dec_d) * 60.0 - dec_m) * 60.0

    hms = f"{ra_h:02.0f}{ra_m:02.0f}{ra_s:05.2f}"
    dms = f"{dec_d:+03.0f}{dec_m:02.0f}{dec_s:04.1f}"

    return prefix + hms + dms


def make_photometry(light_curves: list, drop_flagged: bool = False):
    """
    Make a pandas.DataFrame with photometry

    :param light_curves: list of photometric time series
    :param drop_flagged: drop data points with catflags!=0
    :return:
    """
    dfs = []
    for light_curve in light_curves:
        if len(light_curve["data"]):
            df = pd.DataFrame.from_records(light_curve["data"])
            df["fid"] = light_curve["filter"]
            dfs.append(df)

    df_light_curve = pd.concat(dfs, ignore_index=True, sort=False)

    ztf_filters = {1: "ztfg", 2: "ztfr", 3: "ztfi"}
    df_light_curve["ztf_filter"] = df_light_curve["fid"].apply(lambda x: ztf_filters[x])
    df_light_curve["magsys"] = "ab"
    df_light_curve["zp"] = 23.9
    df_light_curve["mjd"] = df_light_curve["hjd"] - 2400000.5

    df_light_curve["mjd"] = df_light_curve["mjd"].apply(lambda x: np.float64(x))
    df_light_curve["mag"] = df_light_curve["mag"].apply(lambda x: np.float32(x))
    df_light_curve["magerr"] = df_light_curve["magerr"].apply(lambda x: np.float32(x))

    # filter out flagged data:
    if drop_flagged:
        mask_not_flagged = df_light_curve["catflags"] == 0
        df_light_curve = df_light_curve.loc[mask_not_flagged]

    return df_light_curve


class ArchiveCatalogsHandler(BaseHandler):
    @auth_or_token
    def get(self):
        """
        ---
        summary: Retrieve available catalog names from Kowalski/Gloria
        tags:
          - archive
          - kowalski
        responses:
          200:
            description: retrieved catalog names
            content:
              application/json:
                schema:
                  allOf:
                    - $ref: '#/components/schemas/Success'
                    - type: object
                      properties:
                        data:
                          type: array
                          items:
                            type: str
                          description: "array of catalog names"
          400:
            content:
              application/json:
                schema: Error
        """
        query = {"query_type": "info", "query": {"command": "catalog_names"}}
        if gloria is None:
            return self.error("Gloria connection unavailable.")
        catalog_names = gloria.query(query=query).get("data")
        return self.success(data=catalog_names)


class CrossMatchHandler(BaseHandler):
    @auth_or_token
    def get(self):
        """
        ---
        summary: Retrieve data from available catalogs on Kowalski/Gloria by position
        tags:
          - archive
          - kowalski
        parameters:
          - in: query
            name: ra
            required: true
            schema:
              type: float
            description: RA in degrees
          - in: query
            name: dec
            required: true
            schema:
              type: float
            description: Dec. in degrees
          - in: query
            name: radius
            required: true
            schema:
              type: float
            description: Maximum distance in `radius_units` from specified (RA, Dec) (capped at 1 deg)
          - in: query
            name: radius_units
            required: true
            schema:
              type: string
              enum: [deg, arcmin, arcsec]
            description: Distance units (either "deg", "arcmin", or "arcsec")
        responses:
          200:
            description: retrieved source data
            content:
              application/json:
                schema:
                  allOf:
                    - $ref: '#/components/schemas/Success'
                    - type: object
                      properties:
                        data:
                          type: object
                          description: "cross matched sources per catalog"
          400:
            content:
              application/json:
                schema: Error
        """
        query = {"query_type": "info", "query": {"command": "catalog_names"}}
        if gloria is None:
            return self.error("Gloria connection unavailable.")
        available_catalog_names = gloria.query(query=query).get("data")
        # expose all but the ZTF/PTF-related catalogs
        catalogs = [
            catalog
            for catalog in available_catalog_names
            if not catalog.startswith("ZTF")
            and not catalog.startswith("PTF")
            and not catalog.startswith("PGIR")
            and not catalog.startswith("WNTR")
        ]
        if len(catalogs) == 0:
            return self.error("No catalogs available to run cross-match against.")

        # executing a cone search
        ra = self.get_query_argument("ra", None)
        dec = self.get_query_argument("dec", None)
        radius = self.get_query_argument("radius", None)
        radius_units = self.get_query_argument("radius_units", None)

        position_tuple = (ra, dec, radius, radius_units)

        if not all(position_tuple) and any(position_tuple):
            # incomplete positional arguments? throw errors, since
            # either all or none should be provided
            if ra is None:
                return self.error("Missing required parameter: ra")
            if dec is None:
                return self.error("Missing required parameter: dec")
            if radius is None:
                return self.error("Missing required parameter: radius")
            if radius_units is None:
                return self.error("Missing required parameter: radius_units")
        if all(position_tuple):
            # complete positional arguments? run "near" query
            if radius_units not in ["deg", "arcmin", "arcsec"]:
                return self.error(
                    "Invalid radius_units value. Must be one of either "
                    "'deg', 'arcmin', or 'arcsec'."
                )
            try:
                ra = float(ra)
                dec = float(dec)
                radius = float(radius)
            except ValueError:
                return self.error("Invalid (non-float) value provided.")
            if not (0 <= ra < 360):
                return self.error(
                    "Invalid R.A. value provided: must be 0 <= R.A. [deg] < 360"
                )
            if not (-90 <= dec <= 90):
                return self.error(
                    "Invalid Decl. value provided: must be -90 <= Decl. [deg] <= 90"
                )
            if (
                (radius_units == "deg" and radius > 1)
                or (radius_units == "arcmin" and radius > 60)
                or (radius_units == "arcsec" and radius > 3600)
            ):
                return self.error("Radius must be <= 1.0 deg")

            query = {
                "query_type": "near",
                "query": {
                    "max_distance": radius,
                    "distance_units": radius_units,
                    "radec": {"query_coords": [ra, dec]},
                    "catalogs": {
                        catalog: {
                            "filter": {},
                            "projection": {},
                        }
                        for catalog in catalogs
                    },
                },
                "kwargs": {
                    "max_time_ms": 10000,
                    "limit": 1000,
                },
            }

            response = gloria.query(query=query)
            if response.get("status", "error") == "success":
                # unpack the result
                data = {
                    catalog: query_coords["query_coords"]
                    for catalog, query_coords in response.get("data").items()
                }
                # stringify _id's and normalize positional data
                for catalog, sources in data.items():
                    for source in sources:
                        source["_id"] = str(source["_id"])
                        source["ra"] = (
                            source["coordinates"]["radec_geojson"]["coordinates"][0]
                            + 180
                        )
                        source["dec"] = source["coordinates"]["radec_geojson"][
                            "coordinates"
                        ][1]
                return self.success(data=data)

            return self.error(response.get("message"))


class ScopeFeaturesHandler(BaseHandler):
    @auth_or_token
    def post(self):
        """
        ---
        summary: Retrieve archival SCoPe features from Kowalski/Gloria by position, post as annotation
        tags:
          - features
          - kowalski
        parameters:
          - in: query
            name: id
            required: true
            schema:
              type: str
            description: object ID
          - in: query
            name: ra
            required: true
            schema:
              type: float
            description: RA in degrees
          - in: query
            name: dec
            required: true
            schema:
              type: float
            description: Dec. in degrees
          - in: query
            name: catalog
            required: true
            schema:
              type: str
          - in: query
            name: radius
            required: true
            schema:
              type: float
            description: Max distance from specified (RA, Dec) (capped at 1 deg)
          - in: query
            name: radius_units
            required: true
            schema:
              type: string
            description: Distance units (either "deg", "arcmin", or "arcsec")
        responses:
          200:
            content:
              application/json:
                schema: Success
          400:
            content:
              application/json:
                schema: Error
        """
        query = {"query_type": "info", "query": {"command": "catalog_names"}}
        if gloria is None:
            return self.error("Gloria connection unavailable.")
        available_catalog_names = gloria.query(query=query).get("data")
        # expose only the ZTF features for now
        available_catalogs = [
            catalog
            for catalog in available_catalog_names
            if "ZTF_source_features" in catalog
        ]

        data = self.get_json()

        # executing a cone search
        obj_id = data.pop("id")
        ra = data.pop("ra")
        dec = data.pop("dec")
        catalog = data.pop("catalog", "ZTF_source_features_DR5")
        radius = data.pop("radius", 2)
        radius_units = data.pop("radius_units", "arcsec")

        if catalog not in available_catalogs:
            return self.error(f"Catalog {catalog} not available")

        position_tuple = (ra, dec, radius, radius_units)

        if not all(position_tuple) and any(position_tuple):
            # incomplete positional arguments? throw errors, since
            # either all or none should be provided
            if ra is None:
                return self.error("Missing required parameter: ra")
            if dec is None:
                return self.error("Missing required parameter: dec")
            if radius is None:
                return self.error("Missing required parameter: radius")
            if radius_units is None:
                return self.error("Missing required parameter: radius_units")
        if all(position_tuple):
            # complete positional arguments? run "near" query
            if radius_units not in ["deg", "arcmin", "arcsec"]:
                return self.error(
                    "Invalid radius_units value. Must be one of either "
                    "'deg', 'arcmin', or 'arcsec'."
                )
            try:
                ra = float(ra)
                dec = float(dec)
                radius = float(radius)
            except ValueError:
                return self.error("Invalid (non-float) value provided.")
            if (
                (radius_units == "deg" and radius > 1)
                or (radius_units == "arcmin" and radius > 60)
                or (radius_units == "arcsec" and radius > 3600)
            ):
                return self.error("Radius must be <= 1.0 deg")

            # grab id's first
            query = {
                "query_type": "near",
                "query": {
                    "max_distance": radius,
                    "distance_units": radius_units,
                    "radec": {"query_coords": [ra, dec]},
                    "catalogs": {
                        catalog: {
                            "filter": {},
                            "projection": {"_id": 1},
                        }
                    },
                },
                "kwargs": {
                    "max_time_ms": 10000,
                    "limit": 1000,
                },
            }

            response = gloria.query(query=query)
            with self.Session() as session:
                obj = session.scalars(
                    Obj.select(self.current_user).where(Obj.id == obj_id)
                ).first()
                if obj is None:
                    return self.error(
                        f'Cannot find source with id "{obj_id}". ', status=403
                    )

                group_ids = [g.id for g in self.current_user.accessible_groups]
                groups = session.scalars(
                    Group.select(self.current_user).where(Group.id.in_(group_ids))
                ).all()

                if {g.id for g in groups} != set(group_ids):
                    return self.error(
                        f"Cannot find one or more groups with IDs: {group_ids}.",
                        status=403,
                    )

                author = self.associated_user_object

                if response.get("status", "error") == "success":
                    light_curve_ids = [
                        item["_id"]
                        for item in response.get("data")[catalog]["query_coords"]
                    ]
                    if len(light_curve_ids) == 0:
                        return self.success(data=[])

                    # return features for the first ID
                    filter = {"_id": {"$in": [light_curve_ids[0]]}}
                    query = {
                        "query_type": "find",
                        "query": {
                            "catalog": catalog,
                            "filter": filter,
                            "projection": {
                                "ad": 1,
                                "chi2red": 1,
                                "i60r": 1,
                                "i70r": 1,
                                "i80r": 1,
                                "i90r": 1,
                                "inv_vonneumannratio": 1,
                                "iqr": 1,
                                "median": 1,
                                "median_abs_dev": 1,
                                "n": 1,
                                "norm_excess_var": 1,
                                "norm_peak_to_peak_amp": 1,
                                "roms": 1,
                                "skew": 1,
                                "smallkurt": 1,
                                "stetson_j": 1,
                                "stetson_k": 1,
                                "sw": 1,
                                "welch_i": 1,
                                "wmean": 1,
                                "wstd": 1,
                                "_id": 1,
                                "field": 1,
                                "ccd": 1,
                                "quad": 1,
                            },
                        },
                    }

                    response = gloria.query(query=query)
                    if response.get("status", "error") == "success":
                        features = response.get("data")[0]

                        annotations = []
                        annotation_data = {}
                        for key in features.keys():
                            value = features[key]
                            if not pd.isnull(value):
                                if key in ["_id", "field", "ccd", "quad", "n"]:
                                    value = int(value)
                                annotation_data[key] = value

                        if annotation_data:
                            annotation = Annotation(
                                data=annotation_data,
                                obj_id=obj_id,
                                origin=catalog,
                                author=author,
                                groups=groups,
                            )

                            annotations.append(annotation)

                        if len(annotations) == 0:
                            return self.error("No SCoPe features available.")

                        session.add_all(annotations)

                        try:
                            session.commit()
                        except IntegrityError:
                            return self.error("Annotation already posted.")

                        self.push_all(
                            action="skyportal/REFRESH_SOURCE",
                            payload={"obj_key": obj.internal_key},
                        )
                        return self.success()

                return self.error(response.get("message"))


class ArchiveHandler(BaseHandler):
    @auth_or_token
    def get(self):
        """
        ---
        summary: Retrieve archival light curve data from Kowalski/Gloria by position
        tags:
          - archive
          - kowalski
        parameters:
          - in: query
            name: catalog
            required: true
            schema:
              type: str
          - in: query
            name: ra
            required: true
            schema:
              type: float
            description: RA in degrees
          - in: query
            name: dec
            required: true
            schema:
              type: float
            description: Dec. in degrees
          - in: query
            name: radius
            required: true
            schema:
              type: float
            description: Max distance from specified (RA, Dec) (capped at 1 deg)
          - in: query
            name: radius_units
            required: true
            schema:
              type: string
            description: Distance units (either "deg", "arcmin", or "arcsec")
        responses:
          200:
            description: retrieved light curve data
            content:
              application/json:
                schema:
                  allOf:
                    - $ref: '#/components/schemas/Success'
                    - type: object
                      properties:
                        data:
                          type: array
                          items:
                            type: object
                          description: "array of light curves"
          400:
            content:
              application/json:
                schema: Error
        """
        query = {"query_type": "info", "query": {"command": "catalog_names"}}
        if gloria is None:
            return self.error("Gloria connection unavailable.")
        available_catalog_names = gloria.query(query=query).get("data")
        # expose only the ZTF light curves for now
        available_catalogs = [
            catalog for catalog in available_catalog_names if "ZTF_sources" in catalog
        ]

        # allow access to public data only by default
        program_id_selector = {1}

        with self.Session():
            for stream in self.associated_user_object.streams:
                if "ztf" in stream.name.lower():
                    program_id_selector.update(set(stream.altdata.get("selector", [])))

        program_id_selector = list(program_id_selector)

        catalog = self.get_query_argument("catalog")
        if catalog not in available_catalogs:
            return self.error(f"Catalog {catalog} not available")

        # executing a cone search
        ra = self.get_query_argument("ra", None)
        dec = self.get_query_argument("dec", None)
        radius = self.get_query_argument("radius", None)
        radius_units = self.get_query_argument("radius_units", None)

        position_tuple = (ra, dec, radius, radius_units)

        if not all(position_tuple) and any(position_tuple):
            # incomplete positional arguments? throw errors, since
            # either all or none should be provided
            if ra is None:
                return self.error("Missing required parameter: ra")
            if dec is None:
                return self.error("Missing required parameter: dec")
            if radius is None:
                return self.error("Missing required parameter: radius")
            if radius_units is None:
                return self.error("Missing required parameter: radius_units")
        if all(position_tuple):
            # complete positional arguments? run "near" query
            if radius_units not in ["deg", "arcmin", "arcsec"]:
                return self.error(
                    "Invalid radius_units value. Must be one of either "
                    "'deg', 'arcmin', or 'arcsec'."
                )
            try:
                ra = float(ra)
                dec = float(dec)
                radius = float(radius)
            except ValueError:
                return self.error("Invalid (non-float) value provided.")
            if (
                (radius_units == "deg" and radius > 1)
                or (radius_units == "arcmin" and radius > 60)
                or (radius_units == "arcsec" and radius > 3600)
            ):
                return self.error("Radius must be <= 1.0 deg")

            # grab id's first
            query = {
                "query_type": "near",
                "query": {
                    "max_distance": radius,
                    "distance_units": radius_units,
                    "radec": {"query_coords": [ra, dec]},
                    "catalogs": {
                        catalog: {
                            "filter": {},
                            "projection": {"_id": 1},
                        }
                    },
                },
                "kwargs": {
                    "max_time_ms": 10000,
                    "limit": 1000,
                },
            }

            response = gloria.query(query=query)
            if response.get("status", "error") == "success":
                light_curve_ids = [
                    item["_id"]
                    for item in response.get("data")[catalog]["query_coords"]
                ]
                if len(light_curve_ids) == 0:
                    return self.success(data=[])

                query = {
                    "query_type": "aggregate",
                    "query": {
                        "catalog": catalog,
                        "pipeline": [
                            {"$match": {"_id": {"$in": light_curve_ids}}},
                            {
                                "$project": {
                                    "_id": 1,
                                    "ra": 1,
                                    "dec": 1,
                                    "filter": 1,
                                    "meanmag": 1,
                                    "vonneumannratio": 1,
                                    "refchi": 1,
                                    "refmag": 1,
                                    "refmagerr": 1,
                                    "iqr": 1,
                                    "data": {
                                        "$filter": {
                                            "input": "$data",
                                            "as": "item",
                                            "cond": {
                                                "$in": [
                                                    "$$item.programid",
                                                    program_id_selector,
                                                ]
                                            },
                                        }
                                    },
                                }
                            },
                        ],
                    },
                }
                response = gloria.query(query=query)
                if response.get("status", "error") == "success":
                    light_curves = response.get("data")
                    return self.success(data=light_curves)

            return self.error(response.get("message"))

    @permissions(["Upload data"])
    def post(self):
        """
        ---
        description: Post ZTF light curve data from a Kowalski instance to SkyPortal
        tags:
          - archive
          - kowalski
        requestBody:
          content:
            application/json:
              schema:
                allOf:
                  - type: object
                    properties:
                      obj_id:
                        type: str
                        description: "target obj_id to save photometry to. create new if None."
                      catalog:
                        type: str
                        description: "Kowalski catalog name to pull light curves from."
                        required: true
                      light_curve_ids:
                        type: array
                        items:
                          type: integer
                        description: "Light curves IDs in catalog on Kowalski."
                        required: true
                        minItems: 1
                      group_ids:
                        type: array
                        items:
                          type: integer
                        description: "group ids to save source to. defaults to all user groups"
                        required: false
                        minItems: 1
        responses:
          200:
            content:
              application/json:
                schema:
                  allOf:
                    - $ref: '#/components/schemas/Success'
                    - type: object
                      properties:
                        data:
                          type: object
                          properties:
                            obj_id:
                              type: string
                              description: The SkyPortal Obj the light curve was posted to.
          400:
            content:
              application/json:
                schema: Error
        """
        data = self.get_json()

        if gloria is None:
            return self.error("Gloria connection unavailable.")

        obj_id = data.pop("obj_id", None)
        catalog = data.pop("catalog", None)
        light_curve_ids = data.pop("light_curve_ids", None)
        group_ids = data.pop("group_ids", None)

        if obj_id is None and (group_ids is None or len(group_ids) == 0):
            return self.error("Parameter group_ids is required if obj_id is not set")
        if catalog is None:
            return self.error("Missing required parameter: catalog")
        if light_curve_ids is None or len(light_curve_ids) == 0:
            return self.error("Bad required parameter: light_curve_ids")

        # allow access to public data only by default
        program_id_selector = {1}
        with self.Session():
            for stream in self.associated_user_object.streams:
                if "ztf" in stream.name.lower():
                    program_id_selector.update(set(stream.altdata.get("selector", [])))
        program_id_selector = list(program_id_selector)

        # get data from Kowalski/Gloria
        query = {
            "query_type": "aggregate",
            "query": {
                "catalog": catalog,
                "pipeline": [
                    {"$match": {"_id": {"$in": light_curve_ids}}},
                    {
                        "$project": {
                            "_id": 1,
                            "ra": 1,
                            "dec": 1,
                            "filter": 1,
                            "meanmag": 1,
                            "vonneumannratio": 1,
                            "refchi": 1,
                            "refmag": 1,
                            "refmagerr": 1,
                            "iqr": 1,
                            "data": {
                                "$filter": {
                                    "input": "$data",
                                    "as": "item",
                                    "cond": {
                                        "$in": [
                                            "$$item.programid",
                                            program_id_selector,
                                        ]
                                    },
                                }
                            },
                        }
                    },
                ],
            },
        }
        response = gloria.query(query=query)
        if response.get("status", "error") == "error":
            return self.error(response.get("message"))

        light_curves = response.get("data")
        if len(light_curves) == 0:
            return self.error("No data found for requested light_curve_ids")

        # generate a temporary token
        token_name = str(uuid.uuid4())
        token_id = create_token(
            ACLs=self.associated_user_object.permissions,
            user_id=self.associated_user_object.id,
            name=token_name,
        )

        with self.Session() as session:
            user = self.current_user

            try:
                ra_mean = float(
                    np.mean(
                        [
                            light_curve["ra"]
                            for light_curve in light_curves
                            if light_curve.get("ra") is not None
                        ]
                    )
                )
                dec_mean = float(
                    np.mean(
                        [
                            light_curve["dec"]
                            for light_curve in light_curves
                            if light_curve.get("dec") is not None
                        ]
                    )
                )

                if obj_id is None:
                    # generate position-based name if obj_id not set
                    obj_id = radec_to_iau_name(ra_mean, dec_mean, prefix="ZTFJ")

                # create new source, reset obj_id
                sources = session.scalars(
                    Source.select(session.user_or_token).where(Source.obj_id == obj_id)
                ).all()
                num_sources = len(sources)
                is_source = num_sources > 0

                if is_source:
                    log(f"Source {obj_id} exists... updating photometry.")
                else:
                    post_source_data = {
                        "id": obj_id,
                        "ra": ra_mean,
                        "dec": dec_mean,
                        "group_ids": group_ids,
                        "origin": "Fritz",
                    }
                    post_source(post_source_data, user.id, session)

                # post photometry to obj_id; drop flagged data
                df_photometry = make_photometry(light_curves, drop_flagged=True)

                # ZTF instrument id:
                ztf_instrument = session.scalars(
                    Instrument.select(session.user_or_token).where(
                        Instrument.name == "ZTF"
                    )
                ).first()
                if ztf_instrument is None:
                    return self.error("ZTF instrument not found in the system")
                instrument_id = ztf_instrument.id

                photometry = {
                    "obj_id": obj_id,
                    "instrument_id": instrument_id,
                    "mjd": df_photometry["mjd"].tolist(),
                    "mag": df_photometry["mag"].tolist(),
                    "magerr": df_photometry["magerr"].tolist(),
                    "limiting_mag": df_photometry["zp"].tolist(),
                    "magsys": df_photometry["magsys"].tolist(),
                    "filter": df_photometry["ztf_filter"].tolist(),
                    "ra": df_photometry["ra"].tolist(),
                    "dec": df_photometry["dec"].tolist(),
                }

                # handle data access permissions
                ztf_program_id_to_stream_id = dict()
                streams = session.scalars(Stream.select(session.user_or_token)).all()
                if streams is None:
                    return self.error("Failed to get programid to stream_id mapping")
                for stream in streams:
                    if stream.name == "ZTF Public":
                        ztf_program_id_to_stream_id[1] = stream.id
                    if stream.name == "ZTF Public+Partnership":
                        ztf_program_id_to_stream_id[2] = stream.id
                    if stream.name == "ZTF Public+Partnership+Caltech":
                        # programid=0 is engineering data
                        ztf_program_id_to_stream_id[0] = stream.id
                        ztf_program_id_to_stream_id[3] = stream.id
                df_photometry["stream_ids"] = df_photometry["programid"].apply(
                    lambda x: ztf_program_id_to_stream_id[x]
                )
                photometry["stream_ids"] = df_photometry["stream_ids"].tolist()

                if len(photometry.get("mag", ())) > 0:
                    add_external_photometry(photometry, self.current_user)

            finally:
                # always attempt deleting the temporary token
                delete_token(token_id)

            return self.success(data={"obj_id": obj_id})
