open("SIDE_EFFECT_SHOULD_NOT_EXIST", "w").write("executed")
raise RuntimeError("importing this module must never happen")
