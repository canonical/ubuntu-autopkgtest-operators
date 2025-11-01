import json
from datetime import datetime
from pathlib import Path

from .utils import get_supported_releases


def populate_dummy_db(db_con):
    supported_releases = get_supported_releases()

    c = db_con.cursor()
    tests = [
        (1, supported_releases[0], "amd64", "hello"),
        (2, supported_releases[1], "amd64", "hello"),
        (3, supported_releases[0], "ppc64el", "hello"),
        (4, supported_releases[1], "ppc64el", "hello"),
        (5, supported_releases[2], "amd64", "hello"),
        (6, supported_releases[2], "amd64", "hello2"),
        (7, supported_releases[2], "arm64", "hello2"),
        (8, supported_releases[3], "amd64", "hello2"),
        (9, supported_releases[3], "arm64", "hello2"),
        (10, supported_releases[0], "amd64", "vim"),
    ]
    c.executemany("INSERT INTO test values(?, ?, ?, ?)", tests)
    results = [
        # fmt: off
        # test_id | run_id | version | trigger | duration | exit_code | requester | env
        (1, datetime.now(), "1.2.3", "hello/1.2.3", 42, 0, "hyask", ""),
        (
            1,
            datetime.now(),
            "1.2.3",
            "hello/1.2.3",
            42,
            2,
            "hyask",
            "all-proposed=1",
        ),
        (2, datetime.now(), "1.2.3", "hello/1.2.3", 42, 4, "", ""),
        (3, datetime.now(), "1.2.3", "hello/1.2.3", 42, 6, "", ""),
        (4, datetime.now(), "1.2.3", "hello/1.2.3", 42, 8, "", ""),
        (5, datetime.now(), "1.2.3", "hello/1.2.3", 42, 12, "", ""),
        (6, datetime.now(), "2.0.0", "hello/1.2.3", 142, 14, "", ""),
        (7, datetime.now(), "2.0.0", "hello/1.2.3", 142, 16, "", ""),
        (8, datetime.now(), "2.0.0", "hello/1.2.3", 142, 20, "", ""),
        (9, datetime.now(), "2.0.0", "hello/1.2.3", 142, 0, "", ""),
        (
            10,
            datetime.now(),
            "2:9.1.0016-1",
            "vim/2:9.1.0016-1",
            1142,
            0,
            "",
            "",
        ),
        # fmt: on
    ]
    c.executemany("INSERT INTO result values(?, ?, ?, ?, ?, ?, ?, ?, ?)", results)
    db_con.commit()


def populate_dummy_amqp_cache(path: Path):
    supported_releases = get_supported_releases()
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        json.dump(
            {
                "arches": ["amd64", "ppc64el"],
                "queues": {
                    "ubuntu": {
                        supported_releases[0]: {
                            "amd64": {
                                "size": 2,
                                "requests": [
                                    'hello\n{"triggers": ["hello/1.2.3ubuntu2"], "submit-time": "2024-02-22 01:56:14"}',
                                    'hello\n{"triggers": ["hello/1.2.3ubuntu1"], "submit-time": "2024-02-22 01:55:03"}',
                                ],
                            }
                        }
                    },
                    "huge": {
                        supported_releases[1]: {
                            "amd64": {
                                "size": 1,
                                "requests": [
                                    'hello\n{"triggers": ["migration-reference/0"], "submit-time": "2024-02-22 01:55:03"}',
                                ],
                            }
                        }
                    },
                    "ppa": {
                        supported_releases[2]: {
                            "amd64": {
                                "size": 2,
                                "requests": [
                                    'hello\n{"triggers": ["hello/1.2.4~ppa1"], "submit-time": "2024-02-22 01:55:03"}',
                                    'hello2\n{"triggers": ["hello2/2.0.0~ppa1"], "submit-time": "2024-02-22 01:55:03"}',
                                ],
                            }
                        }
                    },
                    "upstream": {
                        supported_releases[3]: {
                            "amd64": {
                                "size": 1,
                                "requests": [
                                    'hello\n{"triggers": ["hello/1.2.4~ppa1"], "submit-time": "2024-02-22 01:55:03"}',
                                ],
                            }
                        }
                    },
                },
            },
            f,
        )


def populate_dummy_running_cache(path: Path):
    supported_releases = get_supported_releases()
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        json.dump(
            {
                "hello": {
                    "hello-hash0": {
                        supported_releases[0]: {
                            "amd64": [
                                {
                                    "submit-time": "2024-02-21 11:00:51",
                                    "triggers": [
                                        "hello/1.2.3",
                                        "hello2/2.2.2",
                                    ],
                                },
                                3204,
                                """
3192s hello/test_XYZ.hello ..................                       [ 84%]
3193s hello/test_XYZ.hello ............................             [ 94%]
""",
                            ]
                        }
                    },
                    "hello-hash1": {
                        supported_releases[0]: {
                            "amd64": [
                                {
                                    "requester": "hyask",
                                    "submit-time": "2024-02-21 11:00:51",
                                    "triggers": [
                                        "hello/1.2.3",
                                    ],
                                },
                                3504,
                                """
3071s hello/test_XYZ.hello .                                        [ 54%]
3153s hello/test_XYZ.hello ......                                   [ 64%]
3271s hello/test_XYZ.hello ..........                               [ 74%]
3292s hello/test_XYZ.hello ..................                       [ 84%]
3493s hello/test_XYZ.hello ............................             [ 94%]
3494s hello/test_XYZ.hello ....................................     [ 98%]
""",
                            ]
                        }
                    },
                    "hello-hash2": {
                        supported_releases[1]: {
                            "amd64": [
                                {
                                    "requester": "hyask",
                                    "submit-time": "2024-02-21 11:00:52",
                                    "triggers": [
                                        "hello/1.2.3",
                                    ],
                                },
                                3614,
                                """
3071s hello/test_XYZ.hello .                                        [ 54%]
3153s hello/test_XYZ.hello ......                                   [ 64%]
3271s hello/test_XYZ.hello ..........                               [ 74%]
3292s hello/test_XYZ.hello ..................                       [ 84%]
3493s hello/test_XYZ.hello ............................             [ 94%]
3594s hello/test_XYZ.hello ....................................     [ 98%]
""",
                            ]
                        }
                    },
                },
                "hello2": {
                    "hello-hash1": {
                        supported_releases[4]: {
                            "amd64": [
                                {
                                    "all-proposed": "1",
                                    "requester": "hyask",
                                    "submit-time": "2024-02-21 11:01:21",
                                    "triggers": [
                                        "hello2/1.2.3-0ubuntu1",
                                    ],
                                },
                                3504,
                                """
3071s hello2/test_XYZ.hello    [ 54%]
3153s hello2/test_XYZ.hello    [ 64%]
3271s hello2/test_XYZ.hello    [ 74%]
3292s hello2/test_XYZ.hello    [ 84%]
3493s hello2/test_XYZ.hello    [ 94%]
3494s hello2/test_XYZ.hello    [ 98%]
""",
                            ]
                        }
                    },
                    "hello-hash2": {
                        supported_releases[4]: {
                            "amd64": [
                                {
                                    "submit-time": "2024-02-21 11:01:21",
                                    "triggers": [
                                        "hello2/1.2.3-0ubuntu2",
                                    ],
                                },
                                5904,
                                """
3071s hello2/test_XYZ.hello    [ 54%]
3153s hello2/test_XYZ.hello    [ 64%]
3271s hello2/test_XYZ.hello    [ 74%]
3292s hello2/test_XYZ.hello    [ 84%]
3493s hello2/test_XYZ.hello    [ 94%]
3494s hello2/test_XYZ.hello    [ 98%]
""",
                            ]
                        }
                    },
                },
            },
            f,
        )
