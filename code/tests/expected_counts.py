"""Expected per-file counts, measured independently of the parsers.

Method (see docs/design_decisions.md): raw byte/line counting on the evidence files,
with no parser code involved:
  sql   : lines starting with INSERT (units); minus INSERT INTO snapshot_scope (observations)
  json  : occurrences of the byte string "resource_id" (units == observations)
  log   : non-blank lines (units); non-blank lines containing "resource_id" (observations)
  txt   : non-blank lines (units); lines starting with "- " (observations)
  pdf   : pypdf page count; one document observation
  png   : Pillow image size; zero observations
Verified equal to the parser output on 2026-10-24 (0 mismatches over 94 files).
"""

# file_id -> (input_units, observations)
EXPECTED_COUNTS = {
    "evidence_01": (104, 36),
    "evidence_02": (41, 41),
    "evidence_03": (44, 41),
    "evidence_04": (1, 0),
    "evidence_05": (61, 61),
    "evidence_06": (112, 37),
    "evidence_07": (30, 30),
    "evidence_08": (36, 36),
    "evidence_09": (1, 0),
    "evidence_10": (59, 59),
    "evidence_11": (118, 36),
    "evidence_12": (29, 29),
    "evidence_13": (39, 39),
    "evidence_14": (1, 0),
    "evidence_15": (49, 49),
    "evidence_16": (93, 36),
    "evidence_17": (36, 36),
    "evidence_18": (1, 1),
    "evidence_19": (1, 0),
    "evidence_20": (63, 63),
    "evidence_21": (128, 41),
    "evidence_22": (71, 71),
    "evidence_23": (1, 0),
    "evidence_24": (50, 50),
    "evidence_25": (128, 43),
    "evidence_26": (32, 32),
    "evidence_27": (1, 1),
    "evidence_28": (1, 0),
    "evidence_29": (85, 85),
    "evidence_30": (110, 38),
    "evidence_31": (74, 74),
    "evidence_32": (1, 0),
    "evidence_33": (89, 89),
    "evidence_34": (136, 44),
    "evidence_35": (28, 28),
    "evidence_36": (46, 46),
    "evidence_37": (1, 0),
    "evidence_38": (85, 85),
    "evidence_39": (114, 40),
    "evidence_40": (31, 31),
    "evidence_41": (39, 39),
    "evidence_42": (1, 0),
    "evidence_43": (51, 51),
    "evidence_44": (125, 41),
    "evidence_45": (75, 75),
    "evidence_46": (1, 0),
    "evidence_47": (49, 49),
    "evidence_48": (103, 36),
    "evidence_49": (34, 34),
    "evidence_50": (20, 20),
    "evidence_51": (4, 4),
    "evidence_52": (32, 32),
    "evidence_53": (305, 300),
    "evidence_54": (7, 7),
    "evidence_55": (1, 1),
    "evidence_56": (2, 2),
    "evidence_57": (3, 3),
    "evidence_58": (1, 1),
    "evidence_59": (2, 2),
    "evidence_60": (1, 0),
    "evidence_61": (10, 5),
    "evidence_62": (34, 34),
    "evidence_63": (24, 17),
    "evidence_64": (54, 52),
    "evidence_65": (32, 32),
    "evidence_66": (31, 23),
    "evidence_67": (4, 2),
    "evidence_68": (31, 31),
    "evidence_69": (26, 18),
    "evidence_70": (50, 48),
    "evidence_71": (33, 33),
    "evidence_72": (28, 21),
    "evidence_73": (68, 66),
    "evidence_74": (30, 30),
    "evidence_75": (32, 24),
    "evidence_76": (48, 46),
    "evidence_77": (31, 31),
    "evidence_78": (26, 19),
    "evidence_79": (60, 58),
    "evidence_80": (29, 29),
    "evidence_81": (32, 24),
    "evidence_82": (54, 52),
    "evidence_83": (36, 36),
    "evidence_84": (34, 26),
    "evidence_85": (46, 44),
    "evidence_86": (28, 28),
    "evidence_87": (27, 19),
    "evidence_88": (46, 44),
    "evidence_89": (30, 30),
    "evidence_90": (1, 0),
    "evidence_91": (62, 60),
    "evidence_92": (40, 40),
    "evidence_93": (25, 17),
    "evidence_94": (58, 56),
}

PDF_PAGES = {"evidence_18": 2, "evidence_27": 2, "evidence_55": 1, "evidence_58": 2}
IMAGE_SIZES = {"evidence_04": (1800, 1200), "evidence_09": (1800, 1100), "evidence_14": (1800, 1150), "evidence_19": (1800, 1150), "evidence_23": (1800, 1100), "evidence_28": (1800, 1200), "evidence_32": (1800, 1150), "evidence_37": (1800, 1100), "evidence_42": (1800, 1100), "evidence_46": (1800, 1150), "evidence_60": (1500, 920)}

# Log dialect of every line of every log file, from a standalone survey regex
# (distinct from the parser regexes): file_id -> {dialect: line count}.
LOG_DIALECTS = {
    "evidence_01": {"logfmt": 104}, "evidence_06": {"jsonl": 112}, "evidence_08": {"logfmt-poslevel": 36},
    "evidence_11": {"syslog": 118}, "evidence_16": {"bracket": 93}, "evidence_21": {"pipe": 128},
    "evidence_25": {"requestid": 128}, "evidence_30": {"logfmt": 110}, "evidence_34": {"jsonl": 136},
    "evidence_39": {"syslog": 114}, "evidence_41": {"logfmt-poslevel": 39}, "evidence_44": {"bracket": 125},
    "evidence_48": {"pipe": 103}, "evidence_53": {"requestid": 305}, "evidence_61": {"logfmt": 10},
    "evidence_64": {"logfmt": 54}, "evidence_67": {"logfmt": 4}, "evidence_70": {"logfmt": 50},
    "evidence_73": {"logfmt": 68}, "evidence_76": {"logfmt": 48}, "evidence_79": {"logfmt": 60},
    "evidence_82": {"logfmt": 54}, "evidence_85": {"logfmt": 46}, "evidence_88": {"logfmt": 46},
    "evidence_91": {"logfmt": 62}, "evidence_94": {"logfmt": 58},
}

TOTAL_INPUT_UNITS = 4187
TOTAL_OBSERVATIONS = 3220
