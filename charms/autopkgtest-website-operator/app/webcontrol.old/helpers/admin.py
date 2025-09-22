DURATION_FACTOR_BEFORE_CONSIDERED_ABNORMAL = 8
DELTA_BETWEEN_LAST_LOG_AND_DURATION = 30  # minutes


def select_abnormally_long_jobs(running_info, get_test_id, db_con):
    selected = {}
    for running_pkg, running_dict in running_info.items():
        for skey, sval in running_dict.items():
            for release, values in sval.items():
                for arch, vals in values.items():
                    try:
                        duration = vals[1]
                        test_id = get_test_id(release, arch, running_pkg)
                        if test_id is None:
                            continue
                        row = db_con.execute(
                            "SELECT AVG(duration) FROM result WHERE test_id=?",
                            (test_id,),
                        )
                        duration_avg = row.fetchone()[0]
                        if (
                            duration_avg
                            and duration
                            > duration_avg * DURATION_FACTOR_BEFORE_CONSIDERED_ABNORMAL
                        ):
                            selected.setdefault(running_pkg, {}).setdefault(
                                skey, {}
                            ).setdefault(release, {}).setdefault(arch, vals)
                    # Whatever happens when trying to select or not the job,
                    # let's not care and handle the next one.
                    except Exception:
                        pass
    return selected


def select_duration_mismatch(running_info):
    selected = {}
    for running_pkg, running_dict in running_info.items():
        for skey, sval in running_dict.items():
            for release, values in sval.items():
                for arch, vals in values.items():
                    try:
                        duration = vals[1]
                        logs = vals[2]
                        # real last log line is a lonely `\n`, thus the `[-2]`
                        last_log_line = logs.split("\n")[-2]
                        # parse the duration on that kind of line:
                        # 1102s package.testsuite ..... run_test1 [0%]
                        #
                        # turning that into a regex makes the parsing of 500
                        # running jobs 200ms slower on production CPU
                        last_printed_duration = int(last_log_line.split(" ")[0][:-1])
                        if (
                            abs(duration - last_printed_duration)
                            > DELTA_BETWEEN_LAST_LOG_AND_DURATION * 60
                        ):
                            selected.setdefault(running_pkg, {}).setdefault(
                                skey, {}
                            ).setdefault(release, {}).setdefault(arch, vals)
                    # Whatever happens when trying to select or not the job,
                    # let's not care and handle the next one.
                    except Exception:
                        pass
    return selected
