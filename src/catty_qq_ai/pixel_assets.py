"""自动生成: 主人参考图 → ASCII 模板 + palette。

由 tools/build_pixel_assets.py 生成。主人想细调像素请直接编辑这里
(改 ASCII rows 或 palette RGB),再 push_zip 推生产即可。
"""

# (rows: list[str], palette: dict[char, (r, g, b)])
Template = tuple[list[str], dict[str, tuple[int, int, int]]]


PAW_TEMPLATES: list[Template] = [
    # paw[0] 13x14
    (
        [
            '.....CC..CC..',
            '....CBBCCABC.',
            '....BAABBAAC.',
            '...CBAABBAACC',
            '.CCCBAABBAACB',
            'CBAACBBCCBCCA',
            'CBAACCCBBCCCA',
            '.CBBCCAAABCCB',
            '..CCCAAAAABCC',
            '..CCAAAAAAABC',
            '..CBAAAAAAAAC',
            '..CBAAAAAAAAC',
            '..CCBABCCAABC',
            '...CCCCCCCCCC',
        ],
        {
            'A': (251, 196, 204),
            'B': (229, 180, 166),
            'C': (215, 169, 146),
        },
    ),
    # paw[1] 14x12
    (
        [
            '..............',
            '....BA..AB....',
            '....BA.BAB....',
            '....BA..AB....',
            '.BB........BB.',
            '.AB........BA.',
            '.....AAAB.....',
            '....BAABAB....',
            '...BABABBAB...',
            '...BBBBBBBB...',
            '...BABBBBAB...',
            '....A...BB....',
        ],
        {
            'A': (247, 216, 222),
            'B': (238, 195, 203),
        },
    ),
    # paw[2] 14x12
    (
        [
            '....CBCCBC....',
            '....AABBAA....',
            '...CAABAAAC...',
            '.CBBAABBAABBC.',
            'CAABBACCACBABC',
            'CAABCCBBCCBAAC',
            '.CBBCAAAACBBC.',
            '..CCAAAAAACC..',
            '..CAAAAAAAAC..',
            '..CAAAAAAAAC..',
            '..CBAABBAABC..',
            '...BBBCCBBB...',
        ],
        {
            'A': (250, 192, 195),
            'B': (220, 176, 158),
            'C': (208, 165, 142),
        },
    ),
    # paw[3] 12x14
    (
        [
            '.....CC.CC..',
            '....BABCBBC.',
            '...CAAABAAB.',
            '..CCBAABAABC',
            '.CABBABCAACB',
            'CAABCCCCCCCA',
            'CBABCCAABCCA',
            '.CCCCAAAABCC',
            '..CCAAAAAABC',
            '..CBAAAAAAAC',
            '..CBAAAAAAAC',
            '..CCBABCBABC',
            '...CBBCCCBCC',
            '....CCC.CCC.',
        ],
        {
            'A': (245, 197, 207),
            'B': (192, 173, 177),
            'C': (160, 159, 159),
        },
    ),
    # paw[4] 14x14
    (
        [
            '.....BC.CC....',
            '....BABCBBC...',
            '...CAAABAAB...',
            '..CCBAABAABC..',
            '.CABBABCABCBAC',
            'CAABCBCCCCCAAB',
            'CBABCCAABCCAAC',
            '.CCCCAAAABCCC.',
            '..CCAAAAAABBC.',
            '..CBAAAAAAABC.',
            '..CBAAAAAAABC.',
            '..CBBABBBABBC.',
            '...CBBCCBBBC..',
            '....CCC.CCC...',
        ],
        {
            'A': (250, 201, 196),
            'B': (234, 178, 162),
            'C': (211, 166, 141),
        },
    ),
    # paw[5] 14x13
    (
        [
            '....CBC.BC....',
            '....BABBAB....',
            '...CAABBAAC...',
            '.CCCAABBAABCC.',
            'CBABBACCABBABC',
            'CAABCCBBCCBAAC',
            '.CBBCBAABCBBC.',
            '..CCBAAAABCC..',
            '..CBAAAAAABC..',
            '..CAAAAAAAAC..',
            '..CBAABBAABC..',
            '...CBBCCBBC...',
            '....CC..CC....',
        ],
        {
            'A': (240, 196, 205),
            'B': (184, 171, 174),
            'C': (153, 153, 153),
        },
    ),
    # paw[6] 14x10
    (
        [
            '.....BB..BB...',
            '.BA..........B',
            '.BA..........A',
            '..B...AAAB...B',
            '.....BAAAAB...',
            '....BAAAABAB..',
            '...BABBABBBAB.',
            '...BABBAAABAB.',
            '....AABBBBAA..',
            '.....A....AB..',
        ],
        {
            'A': (252, 209, 215),
            'B': (242, 195, 205),
        },
    ),
    # paw[7] 14x10
    (
        [
            '.CBBBABCAACBBC',
            'CAABCBCCBCCAAB',
            'CBABCCBABCCAAC',
            '.CBCCAAAABCCCC',
            '..CCAAAAAABCC.',
            '..CBAAAAAAABC.',
            '..CBAAAAAAABC.',
            '..CBAABBBABBC.',
            '...CBBCCCBCC..',
            '....CCC.CCC...',
        ],
        {
            'A': (247, 196, 206),
            'B': (187, 177, 179),
            'C': (160, 158, 158),
        },
    ),
    # paw[8] 14x9
    (
        [
            '....BB..BB....',
            '.BB........BB.',
            '.BB....B...BB.',
            '.....BAAB.....',
            '....BAABAB....',
            '...BABABBAB...',
            '...BBBAABBB...',
            '...BABBBBAB...',
            '....A....A....',
        ],
        {
            'A': (251, 218, 226),
            'B': (238, 195, 205),
        },
    ),
]

# pusheen 26x18
PUSHEEN_TEMPLATE: Template = (
    [
        '.....E......E.............',
        '....DCE....DCE............',
        '....DADEEEEDADEEEEE.......',
        '...DDBCDDDDCBADDDDDD......',
        '..EDABBCCCCABBCCDCADEDD...',
        'EEEDABBBBBBBBBDEDCCDDBDE..',
        '..EDCDCADDACDCBCCBBCCBBDE.',
        'EEDCCCBDEEDBCCDDCABBBBADE.',
        '.EDABABCDDCAABCDCABBBBBCDE',
        '.EDABBBAAAABBBAAABBBBBBACE',
        '.EDABBBBBBBBBBBBBBBBBBBACE',
        '.EDABBBBBBBBBBBBBBBBBBBACE',
        '.EDABBBBBBBBBBBBBBBBBBBACE',
        '.EDABBBBBBBBBBBBBBAAADCACE',
        '..DCABBBBBBBBBBBACDDDDCCD.',
        '...DCAAAAAAAAAAACDDDDCCD..',
        '....ECCCCCCCCCCCDEDCDCD...',
        '.....EEEEEEEEEEEEEEEEE....',
    ],
    {
        'A': (216, 216, 215),
        'B': (202, 202, 201),
        'C': (184, 181, 181),
        'D': (59, 59, 59),
        'E': (0, 0, 0),
    },
)
