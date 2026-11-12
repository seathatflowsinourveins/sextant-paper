# Source import evidence

Baseline: Sextant `bbbe7f5e51159c346b28117bff93361439d2e5c9`, frozen from origin/main when seat C started. Destination harness: `89ab4dcc4f203f3f2a797f7732ca1f00a96f3744` (PR #2).

The approved closure scanner (SHA-256 `5f0a96cec4dcbf7bf4eddf4fdc1c9ece2a0233442b0195dfa2726dce12fd2536`) was regenerated over a git archive at this baseline with the closure report's exact entry points. Compared with the report's `85e6dbedc39b3c28007b0da734c9953dedd72cc2` list, there are zero added or removed paths; only `src/sextant/paper/node_order.py` grew from 738 to 943 lines (+205). The closure is **85 Python files, 24,276 lines, 1,005,694 bytes**. There are no missing local imports or forbidden-lane reaches.

The table also covers the approved ten runtime defaults and four deployment CLI parameters. Each source digest was computed independently from `git show BASELINE:path`, then compared with the copied file. All **99/99** rows are equal. The old execution bridge, excluded lanes, source manifest and source design are absent. This attests content identity, not approval to run; the exact-SHA order/lease/key review remains required.

| Path | Baseline SHA-256 | Copied SHA-256 | Equal |
|---|---|---|---|
| `src/sextant/__init__.py` | `95cf0a98973a78452d7f44c0433059800a4a2f0f39ff9e95f41ddc1d5d6d5e8e` | `95cf0a98973a78452d7f44c0433059800a4a2f0f39ff9e95f41ddc1d5d6d5e8e` | yes |
| `src/sextant/alpaca/__init__.py` | `e7a2f7e643e39b570b6104673b2407cbb73cd9d9d49d55f86b1fa8d14ce559d2` | `e7a2f7e643e39b570b6104673b2407cbb73cd9d9d49d55f86b1fa8d14ce559d2` | yes |
| `src/sextant/alpaca/commission.py` | `d68ac9ae64203189adb85738b238a0c13d2dcb279ff231594fb98a73e3ddcb1a` | `d68ac9ae64203189adb85738b238a0c13d2dcb279ff231594fb98a73e3ddcb1a` | yes |
| `src/sextant/alpaca/config.py` | `97fa0b68abf3e4c205360b3641f82a6e9e37230e25954495cf37d63b70dc765d` | `97fa0b68abf3e4c205360b3641f82a6e9e37230e25954495cf37d63b70dc765d` | yes |
| `src/sextant/alpaca/constants.py` | `532c73f837ae154d7b6d0f8cb185c2abac9c9a7f9c1122168f4c0f7be90105dd` | `532c73f837ae154d7b6d0f8cb185c2abac9c9a7f9c1122168f4c0f7be90105dd` | yes |
| `src/sextant/alpaca/data.py` | `a6e730c8cb04ba4755f146d6cf7be790bd63ebca294a4711b0eb6e23577d6880` | `a6e730c8cb04ba4755f146d6cf7be790bd63ebca294a4711b0eb6e23577d6880` | yes |
| `src/sextant/alpaca/execution.py` | `80a9a156b2f2795af98eb707406cbd3c1a2f73fef774c3a66500cea18574d379` | `80a9a156b2f2795af98eb707406cbd3c1a2f73fef774c3a66500cea18574d379` | yes |
| `src/sextant/alpaca/factories.py` | `40c13f9f89b2d3e1ad418d2fbf5ba4df7150cd79fb9885d33b719fd6c516e60d` | `40c13f9f89b2d3e1ad418d2fbf5ba4df7150cd79fb9885d33b719fd6c516e60d` | yes |
| `src/sextant/alpaca/parsing.py` | `33b37ce00f8801d7a7f2727f7d23b5600be091e673680240c1fee2cb7220d4ee` | `33b37ce00f8801d7a7f2727f7d23b5600be091e673680240c1fee2cb7220d4ee` | yes |
| `src/sextant/alpaca/providers.py` | `ead6a76f71295f0e37df64d00e68f97ce0a5813568b4fae4393a0496828eddd8` | `ead6a76f71295f0e37df64d00e68f97ce0a5813568b4fae4393a0496828eddd8` | yes |
| `src/sextant/alpaca/reports.py` | `de5c72ad998338077563500965853f9e02ef57cb8d06207657bc893b9dcbe314` | `de5c72ad998338077563500965853f9e02ef57cb8d06207657bc893b9dcbe314` | yes |
| `src/sextant/alpaca/stream_facts.py` | `fa69879d51c68f9089a330b18b82a2824c7ac4ca90946c51c750a86cfd1e7ce3` | `fa69879d51c68f9089a330b18b82a2824c7ac4ca90946c51c750a86cfd1e7ce3` | yes |
| `src/sextant/alpaca/trade_updates.py` | `dece350b3b2ee20394b52e0f296f33d214c41872074bac065854c662c1e0b8d6` | `dece350b3b2ee20394b52e0f296f33d214c41872074bac065854c662c1e0b8d6` | yes |
| `src/sextant/alpaca/wire.py` | `cfa04f82fd9ed8c4b9d16e146c10965c2c3e3aff7f86430ee5249eb068bf42a7` | `cfa04f82fd9ed8c4b9d16e146c10965c2c3e3aff7f86430ee5249eb068bf42a7` | yes |
| `src/sextant/data/__init__.py` | `85670db609486dc51225bef6b2ab1554d149e42f1315c8ef37c50512d6047131` | `85670db609486dc51225bef6b2ab1554d149e42f1315c8ef37c50512d6047131` | yes |
| `src/sextant/data/alpaca_history.py` | `6c524123020bf3f0cc1a7ad551831e00160813ea91261db5b2507f0565159fdb` | `6c524123020bf3f0cc1a7ad551831e00160813ea91261db5b2507f0565159fdb` | yes |
| `src/sextant/data/baseline_bars.py` | `d45391ff830d136bc35b08c7a1378b35727f01202aa893f71ce7894c5f181f07` | `d45391ff830d136bc35b08c7a1378b35727f01202aa893f71ce7894c5f181f07` | yes |
| `src/sextant/data/baseline_recipe.py` | `2c50e95de5de6c2dc8b0739b9fb3f96ee0edbaf8eee8e990c3f9416dfa760cf1` | `2c50e95de5de6c2dc8b0739b9fb3f96ee0edbaf8eee8e990c3f9416dfa760cf1` | yes |
| `src/sextant/entry/__init__.py` | `f60a63642aba3776525d02597fb6aa50b04dfc9671861ed62ea6c70cee54f8ab` | `f60a63642aba3776525d02597fb6aa50b04dfc9671861ed62ea6c70cee54f8ab` | yes |
| `src/sextant/entry/indicators.py` | `eff6ce5d2e996750584e243778da09e9f68d11567b73b4b88282d010fe6e06ff` | `eff6ce5d2e996750584e243778da09e9f68d11567b73b4b88282d010fe6e06ff` | yes |
| `src/sextant/entry/orders.py` | `ebe18c46a05ea61a15557ddd139b5decb666ffe592f266e4424e11aa596944c0` | `ebe18c46a05ea61a15557ddd139b5decb666ffe592f266e4424e11aa596944c0` | yes |
| `src/sextant/entry/parameters.py` | `26d79db00c4ef01a923a34cc056cbaaa92d4f5b46977bb7f864d7bc1ae64fa56` | `26d79db00c4ef01a923a34cc056cbaaa92d4f5b46977bb7f864d7bc1ae64fa56` | yes |
| `src/sextant/entry/signals.py` | `591e04872f6f19bd2a86f0fee1eae79bbfb436f5d0ccb2216aac952b0b4d084f` | `591e04872f6f19bd2a86f0fee1eae79bbfb436f5d0ccb2216aac952b0b4d084f` | yes |
| `src/sextant/entry/sizing.py` | `8e6460de76954a169d97c98d2ec5d419f5a92a1c950b98e3e47b499855ba415a` | `8e6460de76954a169d97c98d2ec5d419f5a92a1c950b98e3e47b499855ba415a` | yes |
| `src/sextant/entry/surge.py` | `563f917cb37c77733a1663a0897456f0558f131e7f1f51d38805402d88032ef2` | `563f917cb37c77733a1663a0897456f0558f131e7f1f51d38805402d88032ef2` | yes |
| `src/sextant/execution/__init__.py` | `1f739d306aa9cb6c8c1280eb73e7593144e6b191f77e5bd0dc326df9c202bb5a` | `1f739d306aa9cb6c8c1280eb73e7593144e6b191f77e5bd0dc326df9c202bb5a` | yes |
| `src/sextant/execution/custody.py` | `a2965f99ea0c7acd22ff27dc9f1e612f32aca97bb134af5e9bdc453f9c5d7879` | `a2965f99ea0c7acd22ff27dc9f1e612f32aca97bb134af5e9bdc453f9c5d7879` | yes |
| `src/sextant/execution/pacer.py` | `f3619299c675eb0494d39fae49927270a7e49aeb4a4b4d51e8fb2bd88fcf12fe` | `f3619299c675eb0494d39fae49927270a7e49aeb4a4b4d51e8fb2bd88fcf12fe` | yes |
| `src/sextant/execution/state.py` | `7758109f104e20dbfec37c357ea5a52afa667d6d5349b3ab4e0930cf954d5af0` | `7758109f104e20dbfec37c357ea5a52afa667d6d5349b3ab4e0930cf954d5af0` | yes |
| `src/sextant/exits/__init__.py` | `e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855` | `e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855` | yes |
| `src/sextant/exits/engine.py` | `a1f0497324d3d6e41fcba72c4e456f200af6fafdd8f35f3ba1ab2337aad95d77` | `a1f0497324d3d6e41fcba72c4e456f200af6fafdd8f35f3ba1ab2337aad95d77` | yes |
| `src/sextant/exits/policy.py` | `24584ec57e1be43b650a3e9b6e667342d4b7fea9f4c61293ecff79f560db20c1` | `24584ec57e1be43b650a3e9b6e667342d4b7fea9f4c61293ecff79f560db20c1` | yes |
| `src/sextant/paper/__init__.py` | `edf8f607a3ff71f78f3d8d1d140061e8781bd0cddc2a713002e2e7701d09c8fd` | `edf8f607a3ff71f78f3d8d1d140061e8781bd0cddc2a713002e2e7701d09c8fd` | yes |
| `src/sextant/paper/b_feed.py` | `7df6296097f56d42c1e86181e8e424489830112810957147d585be4f9684faaf` | `7df6296097f56d42c1e86181e8e424489830112810957147d585be4f9684faaf` | yes |
| `src/sextant/paper/b_lifecycle.py` | `75f579a69af8f8d8932a66f27fb99b6ab1eb1395a814b099708bd1aa760a95c2` | `75f579a69af8f8d8932a66f27fb99b6ab1eb1395a814b099708bd1aa760a95c2` | yes |
| `src/sextant/paper/b_protector.py` | `d911c42fcbc735d94a7ea6e18aaa8eb2301c61eec5d9826d83803c87d9afe0a1` | `d911c42fcbc735d94a7ea6e18aaa8eb2301c61eec5d9826d83803c87d9afe0a1` | yes |
| `src/sextant/paper/b_protector_main.py` | `80b1e696ffc888e27aa52473e9b45d94042c1124adb9bed293158a3cff519436` | `80b1e696ffc888e27aa52473e9b45d94042c1124adb9bed293158a3cff519436` | yes |
| `src/sextant/paper/b_width.py` | `c66cee1b974ddaad5e6e17366d02d7b8eabef811ce41b84403e402dde77a5d00` | `c66cee1b974ddaad5e6e17366d02d7b8eabef811ce41b84403e402dde77a5d00` | yes |
| `src/sextant/paper/config.py` | `6e73469a7da238464af43dcea6dad7ddac27ad029f5aec928802b68624577ade` | `6e73469a7da238464af43dcea6dad7ddac27ad029f5aec928802b68624577ade` | yes |
| `src/sextant/paper/decision_record.py` | `265bb1e715618202d026c7ae027399912cdf04e550644a2752c4a7a1fb927460` | `265bb1e715618202d026c7ae027399912cdf04e550644a2752c4a7a1fb927460` | yes |
| `src/sextant/paper/node_order.py` | `90b75fb3d345ece44f948d528539d9c00b95bff99ea56fb6cb995d22b5813b7f` | `90b75fb3d345ece44f948d528539d9c00b95bff99ea56fb6cb995d22b5813b7f` | yes |
| `src/sextant/paper/node_rank.py` | `adb3a3e7afac4a2d6a9b3599a509a121274baff363e955e19c736fbd8b50973c` | `adb3a3e7afac4a2d6a9b3599a509a121274baff363e955e19c736fbd8b50973c` | yes |
| `src/sextant/paper/outside_rth.py` | `0f18adbd017ad55e694a6b4a1b21fb4d6eb4309fe21c3004755c1a6c077d04bf` | `0f18adbd017ad55e694a6b4a1b21fb4d6eb4309fe21c3004755c1a6c077d04bf` | yes |
| `src/sextant/paper/sessions.py` | `66f308f6ba188a854df7ede591edab70e2646d0915574147dccff1f72cd55b19` | `66f308f6ba188a854df7ede591edab70e2646d0915574147dccff1f72cd55b19` | yes |
| `src/sextant/paper/trailing.py` | `2d4e34689c14d4d565591567d5a96323d712ebae8bda98e0e48ac0253db984dc` | `2d4e34689c14d4d565591567d5a96323d712ebae8bda98e0e48ac0253db984dc` | yes |
| `src/sextant/persist.py` | `c149626f034cb55103ec85187c4ba6670213a6a2717056d9f205f3c681f17bcd` | `c149626f034cb55103ec85187c4ba6670213a6a2717056d9f205f3c681f17bcd` | yes |
| `src/sextant/ranking/__init__.py` | `30c93aba78eedd0f79c0c467284cb46c7fc79a945a97b7a123258dadfdc5d426` | `30c93aba78eedd0f79c0c467284cb46c7fc79a945a97b7a123258dadfdc5d426` | yes |
| `src/sextant/ranking/baseline.py` | `0938cd88845040d3b35aec49e12dc979a7bdc5ada5189dda98064c4da875b189` | `0938cd88845040d3b35aec49e12dc979a7bdc5ada5189dda98064c4da875b189` | yes |
| `src/sextant/ranking/capacity.py` | `b1ea48fc5dde05982cb7c5e4ecb6c25ab27b5660e72c82a1f2ad56a6a372c658` | `b1ea48fc5dde05982cb7c5e4ecb6c25ab27b5660e72c82a1f2ad56a6a372c658` | yes |
| `src/sextant/ranking/consumer.py` | `18e5c3c9c13dc7242b3726687f0c71be51adc42d644164adeb79e0ce573acdba` | `18e5c3c9c13dc7242b3726687f0c71be51adc42d644164adeb79e0ce573acdba` | yes |
| `src/sextant/ranking/engine.py` | `7f4d08f18c40e85e8b48181b2bcdf1c3da309a0b44fc1a29b8ec6a3a193caeb0` | `7f4d08f18c40e85e8b48181b2bcdf1c3da309a0b44fc1a29b8ec6a3a193caeb0` | yes |
| `src/sextant/ranking/generation.py` | `c83e655b703552f1cfa128b1b5bccbe7942f2566e9988d70c1f675ac3df72794` | `c83e655b703552f1cfa128b1b5bccbe7942f2566e9988d70c1f675ac3df72794` | yes |
| `src/sextant/ranking/heartbeat.py` | `0122f7c26578e5841028870d2784f7c34896b9059c29abbc9d1454021153a22c` | `0122f7c26578e5841028870d2784f7c34896b9059c29abbc9d1454021153a22c` | yes |
| `src/sextant/ranking/node_actor.py` | `bd987b88334b55fa326564a46e2b781d99eb4e51fc731c63433f506689829b89` | `bd987b88334b55fa326564a46e2b781d99eb4e51fc731c63433f506689829b89` | yes |
| `src/sextant/ranking/node_host.py` | `efffe6224b31d0a36d0657b8dc383d00217ad42a605879cfaa3f8f4bc1d73704` | `efffe6224b31d0a36d0657b8dc383d00217ad42a605879cfaa3f8f4bc1d73704` | yes |
| `src/sextant/ranking/node_main.py` | `14b325aa9de967cfd7d2fda70efd4c057b21694420978333974d87296d627c18` | `14b325aa9de967cfd7d2fda70efd4c057b21694420978333974d87296d627c18` | yes |
| `src/sextant/ranking/node_parameters.py` | `b42e426d2c2faa59cbb59d3905056cd17481bfdb21013ea5c1ea13ebc058fba8` | `b42e426d2c2faa59cbb59d3905056cd17481bfdb21013ea5c1ea13ebc058fba8` | yes |
| `src/sextant/ranking/node_ranking.py` | `ee1c5ab8666a04464ba4be1bdb9b6b262e2480ee15ec7bef8145dc507fdbbc1c` | `ee1c5ab8666a04464ba4be1bdb9b6b262e2480ee15ec7bef8145dc507fdbbc1c` | yes |
| `src/sextant/ranking/node_session.py` | `0a4b037293b1bbd668aac006f41657dd7dde634e0b48d1f927efa55aba99936b` | `0a4b037293b1bbd668aac006f41657dd7dde634e0b48d1f927efa55aba99936b` | yes |
| `src/sextant/ranking/publisher.py` | `71b550a46da6cd7167c1c45ff8be2b5995f2d743e5739f47a273ac43f03f305e` | `71b550a46da6cd7167c1c45ff8be2b5995f2d743e5739f47a273ac43f03f305e` | yes |
| `src/sextant/ranking/rank_gate.py` | `fbabb10a54669590f950f3b9e0eb8c1418f48d9b93f1fddf19512e77c60dd83c` | `fbabb10a54669590f950f3b9e0eb8c1418f48d9b93f1fddf19512e77c60dd83c` | yes |
| `src/sextant/ranking/reader.py` | `c28edc6e218b2bbffc68829c5e48f2639f77269a8d7210b1d2d03c014ded8d8c` | `c28edc6e218b2bbffc68829c5e48f2639f77269a8d7210b1d2d03c014ded8d8c` | yes |
| `src/sextant/ranking/registry.py` | `32740e9e19478ff1ad9b70b25690e17dbb5df4a36447b61a6735228ef43f33f4` | `32740e9e19478ff1ad9b70b25690e17dbb5df4a36447b61a6735228ef43f33f4` | yes |
| `src/sextant/ranking/window.py` | `e6ad8cd58523489aa11f394278fb96da13262dcf888f75c9558aa557f79c5f65` | `e6ad8cd58523489aa11f394278fb96da13262dcf888f75c9558aa557f79c5f65` | yes |
| `src/sextant/risk/__init__.py` | `ea16cab79f24d6fc8592f10735c6815ef7a108d9c1ee8ebf69024046dd287e5a` | `ea16cab79f24d6fc8592f10735c6815ef7a108d9c1ee8ebf69024046dd287e5a` | yes |
| `src/sextant/risk/controls.py` | `ad35bf486b042fce37dfc1f0e751d8bf94a7d093ab36c1e3985a2343c775d636` | `ad35bf486b042fce37dfc1f0e751d8bf94a7d093ab36c1e3985a2343c775d636` | yes |
| `src/sextant/tradingview/__init__.py` | `76c7bfb2598c13eb7327340741a6385a80792737e518ed26ea4530bd2f382bfd` | `76c7bfb2598c13eb7327340741a6385a80792737e518ed26ea4530bd2f382bfd` | yes |
| `src/sextant/tradingview/__main__.py` | `adfbc4eabcd7b4245ac4ecdb5db9c440a2da1f90ced9b25d06096550dfd211ae` | `adfbc4eabcd7b4245ac4ecdb5db9c440a2da1f90ced9b25d06096550dfd211ae` | yes |
| `src/sextant/tradingview/account.py` | `393a0e095fb53cc9fe011f0e993be8d380d7df0d7160ec67442f0799e7e1b7fa` | `393a0e095fb53cc9fe011f0e993be8d380d7df0d7160ec67442f0799e7e1b7fa` | yes |
| `src/sextant/tradingview/auth.py` | `e566ce2a30ccca37eb17ebc531d3341e69d7f8d8822b7c8bcb844d5ff7d65dc1` | `e566ce2a30ccca37eb17ebc531d3341e69d7f8d8822b7c8bcb844d5ff7d65dc1` | yes |
| `src/sextant/tradingview/bars.py` | `adfa40dc7fcbcfdcc45250dbdb4a6ae99a003a4863873dfd03312cab1a645225` | `adfa40dc7fcbcfdcc45250dbdb4a6ae99a003a4863873dfd03312cab1a645225` | yes |
| `src/sextant/tradingview/baseline.py` | `409ff099c17a375ac1a827aa902d06010209e22515a11b8668b86ef689a7c72f` | `409ff099c17a375ac1a827aa902d06010209e22515a11b8668b86ef689a7c72f` | yes |
| `src/sextant/tradingview/bridge.py` | `1f957606fdb67f9158782297c39f25d888c77263cc3537dc4f837ebc590854f4` | `1f957606fdb67f9158782297c39f25d888c77263cc3537dc4f837ebc590854f4` | yes |
| `src/sextant/tradingview/cadence.py` | `3562d8dd68f9104ed970ac830a5e82e38bd9106aa67a3c080fb534856ab864f6` | `3562d8dd68f9104ed970ac830a5e82e38bd9106aa67a3c080fb534856ab864f6` | yes |
| `src/sextant/tradingview/diagnostics.py` | `ec633128bead659e9243aa9f5c6e1cccea3822d4bcaf552a5dd289a8b0c211eb` | `ec633128bead659e9243aa9f5c6e1cccea3822d4bcaf552a5dd289a8b0c211eb` | yes |
| `src/sextant/tradingview/host.py` | `13a8cb2b364c5de7ffca2f9a3b33b4619da226113b2f2ffd98605a8025238bd9` | `13a8cb2b364c5de7ffca2f9a3b33b4619da226113b2f2ffd98605a8025238bd9` | yes |
| `src/sextant/tradingview/inputs.py` | `4c8e9a0dd3bb91386a5c59577cf37ad9b65a128e45bd414fa511d21570f0343c` | `4c8e9a0dd3bb91386a5c59577cf37ad9b65a128e45bd414fa511d21570f0343c` | yes |
| `src/sextant/tradingview/listings.py` | `65982b4781f0f681a5cfc1a7c90197aa04749bcd3a2c8ac8e70847b18d2b996d` | `65982b4781f0f681a5cfc1a7c90197aa04749bcd3a2c8ac8e70847b18d2b996d` | yes |
| `src/sextant/tradingview/loop.py` | `95b85ee289590a5a9acfc6e1262223a6e9fb4734f4f4d72f4d5971698be68522` | `95b85ee289590a5a9acfc6e1262223a6e9fb4734f4f4d72f4d5971698be68522` | yes |
| `src/sextant/tradingview/presets.py` | `06839b45422289a7c8b5c5d16bee384bf030f3738676a0a422e211d3d9bd293f` | `06839b45422289a7c8b5c5d16bee384bf030f3738676a0a422e211d3d9bd293f` | yes |
| `src/sextant/tradingview/prestage.py` | `e5613ed98b215ef3f7c593f078ee1950bcea3b7c092a2a3bbc8bf151d39bda37` | `e5613ed98b215ef3f7c593f078ee1950bcea3b7c092a2a3bbc8bf151d39bda37` | yes |
| `src/sextant/tradingview/producer.py` | `88cb08ed33a60b1d25a340749fdca03e88c3b62a6bcd0a9da2f33f057680045f` | `88cb08ed33a60b1d25a340749fdca03e88c3b62a6bcd0a9da2f33f057680045f` | yes |
| `src/sextant/tradingview/ranked_view.py` | `f3b0fc9927db88b82ea427368a0b099c7faf06506b943a49a9fdc00c5c37d0a1` | `f3b0fc9927db88b82ea427368a0b099c7faf06506b943a49a9fdc00c5c37d0a1` | yes |
| `src/sextant/tradingview/stored_login.py` | `26d702c899d5831f0a17299c82cbb1dd96306b3d4c3c08f9efd60976430fb70a` | `26d702c899d5831f0a17299c82cbb1dd96306b3d4c3c08f9efd60976430fb70a` | yes |
| `src/sextant/tradingview/sync.py` | `4aeac058112dc82a33a52d3c5f6cc8d91f6a88db14519f10e0a735336a70b224` | `4aeac058112dc82a33a52d3c5f6cc8d91f6a88db14519f10e0a735336a70b224` | yes |
| `deploy/listings/parameters.json` | `6547c9b8923f6297fb3b59899bb9aa2a36476cb6cbcd66420e9dbba826a1bee6` | `6547c9b8923f6297fb3b59899bb9aa2a36476cb6cbcd66420e9dbba826a1bee6` | yes |
| `deploy/ranking-node/parameters.json` | `d5c03ad3bca0a405ca5ac07ada51a84a2a4332ea5002f4b3c12286933ff8d315` | `d5c03ad3bca0a405ca5ac07ada51a84a2a4332ea5002f4b3c12286933ff8d315` | yes |
| `lanes/custody-parameters.json` | `e7deb648b05583c4e100afba786e53d8188011573fc8164c21f575630b99d12c` | `e7deb648b05583c4e100afba786e53d8188011573fc8164c21f575630b99d12c` | yes |
| `lanes/limits.yaml` | `6822c41bd4b4b5ef465f186b07a25976a4c83bd7279d2e7cc5d2c9f7fa93db8c` | `6822c41bd4b4b5ef465f186b07a25976a4c83bd7279d2e7cc5d2c9f7fa93db8c` | yes |
| `lanes/paper/b-parameters.json` | `8670b949e34b8733a166050879116de9fdd6ad874f062f5c03e6a81346facd15` | `8670b949e34b8733a166050879116de9fdd6ad874f062f5c03e6a81346facd15` | yes |
| `lanes/tradingview/account.params.json` | `cb309649fa579ee37a4b20351d13964cb54e5418eea5f426d751a90ac6d347d4` | `cb309649fa579ee37a4b20351d13964cb54e5418eea5f426d751a90ac6d347d4` | yes |
| `lanes/tradingview/display-bounds.params.json` | `2f31e38df4eae92cad672299766fc84f6f600873ee8d7ab0f962ffd3164d01ac` | `2f31e38df4eae92cad672299766fc84f6f600873ee8d7ab0f962ffd3164d01ac` | yes |
| `lanes/tradingview/j5-cadence.params.json` | `8537bcf61a09c40811a60899cef130d22a8a405308d8417130e85a5e4798de48` | `8537bcf61a09c40811a60899cef130d22a8a405308d8417130e85a5e4798de48` | yes |
| `lanes/tradingview/k2-ranking.params.json` | `eb048a1749a0b07e81ae239082d4912cc799190fa935c464efde43bbe1500d95` | `eb048a1749a0b07e81ae239082d4912cc799190fa935c464efde43bbe1500d95` | yes |
| `lanes/tradingview/prestage-parameters.json` | `07fa45e954c79084bcd94bfff21e4caba3adba4dae859f0ec0766b09d01f93c1` | `07fa45e954c79084bcd94bfff21e4caba3adba4dae859f0ec0766b09d01f93c1` | yes |
| `lanes/paper/b-entry-parameters.json` | `aabb64e1931de2e2f149cc55b0286d99c0adee6649a094315cc66b8d09ae2fe3` | `aabb64e1931de2e2f149cc55b0286d99c0adee6649a094315cc66b8d09ae2fe3` | yes |
| `lanes/paper/b-gain-lock.params.json` | `8b087d628d4784eec67040cb3419033acd7fcef652fb15d482dad34b397b34f6` | `8b087d628d4784eec67040cb3419033acd7fcef652fb15d482dad34b397b34f6` | yes |
| `lanes/paper/b-reentry-parameters.json` | `a35790ab384f886f9126550ae3eda2f669ba9bf89e03309b516d1b8788f88820` | `a35790ab384f886f9126550ae3eda2f669ba9bf89e03309b516d1b8788f88820` | yes |
| `lanes/paper/shared-node.params.json` | `129568b14897b5b3896b13ff559d2f8bb665cc63063596a9fe2a100d066c7fc7` | `129568b14897b5b3896b13ff559d2f8bb665cc63063596a9fe2a100d066c7fc7` | yes |

## Packaging configuration

The distribution is named `sextant-paper`, while the approved import namespace remains `sextant`. The source build-system pin is retained; `tool.uv.build-backend.module-name = "sextant"` follows the official [uv build backend Modules documentation](https://docs.astral.sh/uv/concepts/build-backend/#modules), read 2026-11-11. An offline wheel build first failed looking for `src/sextant_paper/__init__.py`, then passed with this configuration. No copied source was changed.
