import configparser


def make_conf(engine_dir=None, **values):
    conf = configparser.ConfigParser()
    conf.add_section("Fishnet")
    if engine_dir is not None:
        conf.set("Fishnet", "EngineDir", str(engine_dir))
    for key, value in values.items():
        conf.set("Fishnet", key, str(value))
    return conf
