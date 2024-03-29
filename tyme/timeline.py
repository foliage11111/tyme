"""
Main API for interfacing with timeline internal representation. Timelines
are .hjson files with two fields, "timeline" and "activities". The first is
a mapping between days and lists of occurences of activities. The second is
the activity hierarchy.
"""

import uuid
from collections import defaultdict
from typing import Any, Dict, List, Optional, Tuple

import hjson

import tyme.utils as utils
from tyme.common import *


JSONTimeline = Dict[str, List[Dict[str, str]]]
JSONActivities = Dict[str, Tuple[str, "JSONActivities"]]


class TimelineError(Exception):
    pass


class Timeline:
    """
    Interface to modify timeline data. Create new activity categories, start
    or finish activities, and query statistics about them.
    """

    def __init__(self,
                 user: str = None,
                 timeline: JSONTimeline = None,
                 activities: JSONActivities = None) -> None:
        """
        Creates a timeline with a user. If a user is not specified, then
        the defauly user is used. If a user is specified, then that user's
        timeline is loaded, unless timeline & activities are also passed in.

        Args:
            user (str): the user whose timeline is being loaded/created
            timeline (Optional[JSONTimeline]):
                a timeline to use if `user` doesn't yet exist
            activities (Optional[JSONActivities]):
                an activity hierarchy to use if `user` doesn't yet exist
        """
        if user is None:
            user = Timeline.default_user()
        self.user = user

        if timeline is not None and activities is not None:
            self.timeline = timeline
            self.activities = activities

        elif timeline is None and activities is None:
            user_state = Timeline.load_user_timeline(user)
            self.timeline = user_state["timeline"]
            self.activities = user_state["activities"]

        else:
            raise ValueError(
                "both timeline and activies must have values or be None")

    def recent_activities(self, num: int) -> Dict[str, List[Dict[str, str]]]:
        """
        Returns the `num` most recent activities. The returned object is a
        dictionary with dates as keys and lists of activities as values. Each
        activity is also a dictionary, with the same fields present in the
        timeline.hjson file. The lists are ordered by oldest event first.

        Args:
            num (int): the number of activities to return

        Returns:
            Dict[str, List[Dict[str, str]]]: the `num` most recent activities
        """

        # `activities`: a map from day (str) to a list of timeline entries
        activities: Dict[str, List[Dict[str, str]]] = defaultdict(list)

        # Grab the most recent `num` events
        for day in sorted(self.timeline.keys(), reverse=True):
            for activity in self.timeline[day]:
                # not a real activity, but a link to one on a previous day
                if "previous" in activity:
                    continue

                activities[day].append(activity)

                num -= 1
                if num == 0:
                    return dict(activities)

        return dict(activities)

    def start(self,
              activity: str) -> Optional[Tuple[utils.Timestamp, utils.Timestamp, str]]:
        """
        Completes any ongoing activity and starts a new one.

        Args:
            activity (str): the activity to be started

        Returns:
            Optional[Tuple[utils.Timestamp, utils.Timestamp, str]]:
                information about the activity that was completed in order to
                start this one: start/end/name. If there was no previous activity, this is
                `None`.
        """
        activity_id = self.activity_id(activity)
        if activity_id is None:
            raise TimelineError(f"The activity '{activity}' does not exist.")

        activity_completed: Optional[Tuple[utils.Timestamp,
                                           utils.Timestamp, str]] = None
        if self.current_activity() is not None:
            activity_completed = self.done()

        start_timestamp = utils.utc_now()
        if start_timestamp.date_str not in self.timeline:
            self.timeline[start_timestamp.date_str] = []

        self.timeline[start_timestamp.date_str].append({
            "id": activity_id,
            "name": activity,
            "start": start_timestamp.datetime_str
        })

        return activity_completed

    def done(self) -> Tuple[utils.Timestamp, utils.Timestamp, str]:
        """
        Completes the ongoing activity. There must be an ongoing activity for
        this method to be called successfully.

        Returns:
            Tuple[utils.Timestamp, utils.Timestamp, str]:
                information about the activity that was completed:
                start/end/name.
        """
        # grab the most recent day and the most recent activity on that day
        last_activity = self.timeline[sorted(self.timeline.keys())[-1]][-1]

        start_timestamp = utils.parse(last_activity["start"])
        end_timestamp = utils.utc_now()

        last_activity["end"] = end_timestamp.datetime_str

        # quickly check that start time is not in the future.
        if start_timestamp.date_str > end_timestamp.date_str:
            raise TimelineError("Finishing activity before it was started. "
                                "Maybe system clock is wrong?")

        # fill any days in between the start time and today
        if start_timestamp.date_str != end_timestamp.date_str:
            num_days = end_timestamp.datetime.day - start_timestamp.datetime.day
            for offset in range(1, num_days + 1):
                day = utils.offset_day(start_timestamp, days_offset=offset)
                self.timeline[day] = [
                    {
                        "id": last_activity["id"],
                        "name": last_activity["name"],
                        "start": last_activity["start"],
                        "end": end_timestamp.datetime_str,
                        "previous": "",
                    }
                ]

        return (start_timestamp, end_timestamp, last_activity["name"])

    def current_activity(self) -> Optional[Dict[str, str]]:
        """
        Returns the ongoing activity if there is one. Returns `None` otherwise.

        Returns:
            Optional[Dict[str, str]]:
                The literal JSON that represents this activity.
        """
        if self.timeline == {}:
            return None

        last_activity = self.timeline[sorted(self.timeline.keys())[-1]][-1]

        if "end" in last_activity:
            return None

        return last_activity

    def save(self) -> str:
        """
        Saves this timeline to the default location:

        Returns:
            str: the location of the .hjson file that was saved.
        """
        timeline_file = (TYME_TIMELINES_DIR / self.user).with_suffix(".hjson")

        with open(timeline_file, "w") as timeline:
            hjson.dump({"timeline": self.timeline,
                        "activities": self.activities},
                       timeline)
        return str(timeline_file)

    def new_activity(self, activity, parents=False):
        """
        Creates a new activity. `activity` can either be a single name or a
        path of the form /path/to/activity. If the activity is not a path,
        then a cli interface will come up allowing the user to select where
        in the hierarchy this activity should be created. If a path of the
        form /p1/p2/p3/.../pn is passed in, then p1 through p(n-1) must exist
        in that order and pn will be the new activity.

        If parents is true, the parents of an absolute activity path /p1/.../pn
        will also be created if they do not exist.
        """
        activity_path = activity.split("/")[1:]
        if "" in activity_path:
            raise ValueError("malformed absolute activity path")

        *path, new_activity = activity_path

        current_category = self.activities
        for category in path:
            if category not in current_category:
                if not parents:
                    raise ValueError(f"the activity '{category}' within "
                                     f"'{activity}' does not exist")
                else:
                    # just make a new activity.
                    current_category[category] = (str(uuid.uuid4()), {})

            # [1] is because the first element in each activity is a uuid
            current_category = current_category[category][1]

        current_category[new_activity] = (str(uuid.uuid4()), {})

    def activity_path(self, activity: str) -> Optional[str]:
        """
        Returns the absolute path leading to activity `activity` if there is
        one. Otherwise, return `None`.

        Args:
            activity (str): the activity whose absolute path is desired

        Returns:
            Optional[str]: the absolute path leading to `activity` if there
                is one
        """
        def search(category, path):
            """
            Returns the path if activity is under the sub-tree `category`
            otherwise returns None
            """
            for name, (_, children) in category.items():
                if name == activity:
                    return f"{path}/{activity}"

                potential_path = search(children, f"{path}/{name}")
                if potential_path is not None:
                    return potential_path

            return None

        return search(self.activities, "")

    def activity_id(self, activity: str) -> Optional[str]:
        """
        Returns the uuid4 corresponding to activity `activity` if there is one.
        Otherwise, return `None`.

        Args:
            activity (str): the activity whose id is desired

        Returns:
            Optional[str]: the uuid4 corresponding to `activity` if there is
                one
        """
        def search(category: JSONActivities) -> Optional[str]:
            """
            Returns the id of `activity` if it is under the sub-tree `category`
            otherwise returns None
            """
            for name, (activity_id, children) in category.items():
                if name == activity:
                    return activity_id

                potential_id = search(children)
                if potential_id is not None:
                    return potential_id

            return None

        return search(self.activities)

    @staticmethod
    def make_empty(user: str) -> None:
        """
        Creates an empty timeline for user `user`.

        Args:
            user (str): the user whose timeline is being created
        """
        Timeline(user=user, timeline={}, activities={}).save()

    @staticmethod
    def default_user() -> str:
        """
        Returns the default user.

        Returns:
            str: the name of the default user
        """
        with open(TYME_STATE_FILE) as state_file:
            return hjson.load(state_file)["default_user"]

    @staticmethod
    def load_user_timeline(user: str) -> Any:
        """
        Loads and returns the json object corresponding to a users timeline.
        This will contain two fields "timeline" and "activites", each
        corresponding to a JSONTimeline and JSONActivities object respectively.

        Args:
            str: the user whose timeline is desired

        Returns:
            The json object corresponding to a users timeline
        """
        user_timeline_path = (TYME_TIMELINES_DIR / user).with_suffix(".hjson")
        with open(user_timeline_path) as timeline:
            return hjson.load(timeline)
