# Proof import identity table

Baseline: `bbbe7f5e51159c346b28117bff93361439d2e5c9`. Every listed copied file equals `git show BASELINE:<path>` byte for byte; no repository-path constant changes.

| Path | Upstream and local SHA-256 | Equal |
| --- | --- | --- |
| `lanes/sim/entry-parameters.json` | `6c12efed683eb3de3e9e6d743ed40c786262edac4d25bb2e06d22e4d0317ee00` | yes |
| `lanes/tradingview/presets/ema_cross.pine` | `648ec3830d757a90333792711ac6e19642e7ab2bef315d7bb5b21d31b87bdd37` | yes |
| `lanes/tradingview/presets/exit_levels.pine` | `210733c3312a4497ef8e2ad8e01ff5f607d9b93c724087d0a87ab2fa7e5029ac` | yes |
| `lanes/tradingview/presets/hurst_vpin.pine` | `bec431f2c9e49e062d0a43e1dea20079774b0827d42266b131351eea256c8b7d` | yes |
| `lanes/tradingview/presets/manifest.json` | `b56296438e67176d098174012e6928b232225aabeaaf334414c889924013ad1c` | yes |
| `lanes/tradingview/presets/quote_imbalance.pine` | `86977f1a86f5dc76b808e457fc5ca1849320bda4990094caf8e0a65ac97ad9e7` | yes |
| `tests/fixtures/nasdaqtrader/nasdaqlisted.txt` | `00e0d3a4f7bb2a7bb054f3f756f7a24dca426dcdb9ae8f1eecddfa40678ea9ce` | yes |
| `tests/fixtures/nasdaqtrader/otherlisted.txt` | `11de52b83bbc6137aa8446c4ee1bf4ddeb73981fed021f6dac87a149b9f02a23` | yes |
| `tests/proofs/conftest.py` | `68358daa700c302702389b5f56b50af3b5126757309cdcc514a6de13b373755e` | yes |
| `tests/proofs/custody/__init__.py` | `e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855` | yes |
| `tests/proofs/custody/alpaca_venue.py` | `080dd5898b858322dd34db94b8b9579e5842f5facc58dcb8d8f74fb065e713a8` | yes |
| `tests/proofs/custody/conftest.py` | `5bb882c81405070646fc03adab4066bb4b905cf3013b4ebc82b1f75b1ee67cf1` | yes |
| `tests/proofs/custody/test_alpaca_answers.py` | `4170c046a51677a50f7de612c1b367c6e1a51c6659a0c4ee338d9046df0db0c7` | yes |
| `tests/proofs/custody/test_alpaca_before_order_history.py` | `254c79cbee871b7db7a6d910e08037c5e7ab502a5841896d13a1124a8fdee0f5` | yes |
| `tests/proofs/custody/test_alpaca_client.py` | `72abb3508fabebeca18d3c2ae9796fe84d217e7e8a4517a9957b930cb2b692ea` | yes |
| `tests/proofs/custody/test_alpaca_commission_wiring.py` | `583fd9d148f4dc02538e852d7f02affe0de0fcc45f825166fbbab513b9203143` | yes |
| `tests/proofs/custody/test_alpaca_coverage.py` | `886161965d996d63e068947105e911be12d849398b7550e3160dd2eeb58bb50d` | yes |
| `tests/proofs/custody/test_alpaca_disconnect_cancel_witness.py` | `c9bd2c6a296efebc4856d5da0c509a1ea131da5f3570d44773a179b532fbde54` | yes |
| `tests/proofs/custody/test_alpaca_execution.py` | `449053a41593c624a70c42456b40170741fd00e4cb4e33e4fd1cb35bf4dcc1f0` | yes |
| `tests/proofs/custody/test_alpaca_exit_table_attacks.py` | `b81c6993120cb30c7b036d9bd78e58a94d99ade962d86486329bc3f6c12418a3` | yes |
| `tests/proofs/custody/test_alpaca_exits.py` | `019efd3d9699d88f78b44f4d6fcdc68d016c0e7da5b9719d023ab6a593f99e2d` | yes |
| `tests/proofs/custody/test_alpaca_fill_history.py` | `4b2ba1dcefedf668e088d5534f86b67ff36a0bb83ba038965adde6fc0409aebe` | yes |
| `tests/proofs/custody/test_alpaca_inflight.py` | `863403df9c9ca18b9a3d4046775995aa8b3f7f7d299e4aaf3951ce8d88f6f36f` | yes |
| `tests/proofs/custody/test_alpaca_ingress.py` | `eaa4a13a49f40514592a7b3c910ca4bd606af915287c8da51b65eb29016fbb81` | yes |
| `tests/proofs/custody/test_alpaca_ingress_time.py` | `2b76c13d8ab58000c4ec3a004c093130153170d7736778c59a86dd8f4ece391d` | yes |
| `tests/proofs/custody/test_alpaca_node.py` | `837f1cbd25db84a7a232dfe7a22a345d837bb4d463662fc9f7cc4f6c93d1ff43` | yes |
| `tests/proofs/custody/test_alpaca_node_claims.py` | `a5282555515ec7e4c170d7b903be0bc13d818ec0088cafe451d6e00dbd14e303` | yes |
| `tests/proofs/custody/test_alpaca_node_owner.py` | `932cc8542f992b9ac45ef1cd6a7d4a63bf893de641f1add7921f759b6eb5dc7d` | yes |
| `tests/proofs/custody/test_alpaca_outcomes.py` | `73b4c2e462aaf3d470b82bb58521f605234be0ecc89bab2d6ad02d9ddc624311` | yes |
| `tests/proofs/custody/test_alpaca_owner_rows.py` | `c80d3334eed3ae4cf2aebc4382d0c0847c0ca98d20ef2451fd7015b7e789ef14` | yes |
| `tests/proofs/custody/test_alpaca_paging.py` | `393aa7ee2aff607d6365034592279468a080495075bea8891a6423779a44ec78` | yes |
| `tests/proofs/custody/test_alpaca_paging_row_witness.py` | `2e26b084eed37860e1b4793f63179ce04f11533ab4cf3e32642d2ab292b5dc37` | yes |
| `tests/proofs/custody/test_alpaca_priority.py` | `a1c255a211c32f7f167749986ee485392a9448638d561b6d73b2416c3264730a` | yes |
| `tests/proofs/custody/test_alpaca_response_semantics.py` | `a5198207737e3339e0830db0d64f735986e05328fec0d206ed1c71426f0de6fc` | yes |
| `tests/proofs/custody/test_alpaca_start_cause.py` | `af0a93a089df24ade9a157666fec695442055a471ee589131a4d4ad147eb8c92` | yes |
| `tests/proofs/custody/test_alpaca_successor.py` | `f13754a7a0d0e429040b412989f47322f0c3d8b6aa09feb1cd6e3236e6814b0e` | yes |
| `tests/proofs/custody/test_alpaca_unloaded_history.py` | `756adeaacfc96c4d3db4db59f542a230c9c928e6b1ed49fad5dd33e3eba5c938` | yes |
| `tests/proofs/custody/test_b_attended_entry.py` | `56df3203af13c304b802518a6c42ca2556a883a8792850c11afe8441f7155289` | yes |
| `tests/proofs/custody/test_b_attended_stop.py` | `09a66217219f78c2b19b2a8c5baaffe889dea6b173e466f37b19e836fb044641` | yes |
| `tests/proofs/custody/test_b_order_node.py` | `fd06b274135913ca2c95552c4edc84945636f1c05d05a88cefabd5c933a447c1` | yes |
| `tests/proofs/custody/test_controls.py` | `aa067f41de434ac181521ae48a7a26aeb2ae2795679c3b04c9d369fb10fc5a34` | yes |
| `tests/proofs/custody/test_custody.py` | `21a4221979a49f90cb0ecf1b8c46a8d586824d4468e19692c9e817bd32766a90` | yes |
| `tests/proofs/custody/test_node_cleanup_errors.py` | `fdcfd0b4c38bc7fe704bd3a52abf39447b096f5f15a810aeaece274e078e83f3` | yes |
| `tests/proofs/custody/test_pacer.py` | `b3087a0609207de91de7b50b0c6d4532b2cc86f34c7bbaf6e39511921089c8b9` | yes |
| `tests/proofs/entry/__init__.py` | `e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855` | yes |
| `tests/proofs/entry/conftest.py` | `41b9784de33cf58efdcb3f4c4e80fec758393474a306bf07631587aef58b26e5` | yes |
| `tests/proofs/entry/test_segment_state.py` | `ff17fa04e02d154293a9a4ab743454c9c7635564eaffdc14ebbed373034a7b4b` | yes |
| `tests/proofs/entry/test_signals.py` | `8f422856fb2662e1a4a174680a882e113452ab422ec72efe9c0302f046f3ff4f` | yes |
| `tests/proofs/entry/test_sizing.py` | `fbdfa1ec62d16afb5c9795f8efa164c902b8502c8df7aa9ac71978527737fe03` | yes |
| `tests/proofs/entry/test_sizing_registry.py` | `5ac49e20ffce41d13fa365f4c9e92afe7535b191cccd0223b1a99410ec24eeea` | yes |
| `tests/proofs/test_alpaca_adapter.py` | `5fda51d9e124f65d77608131ad7c45e30d8eae171ca2b00ede555763b67db6ed` | yes |
| `tests/proofs/test_alpaca_commission.py` | `35cfb0fd43ddba226d5a99ade53145c640b1c0bac31da0bd850eacfe1e9a44ad` | yes |
| `tests/proofs/test_alpaca_reports.py` | `1cd186f2bb32165b3bd721ad2714e7800bcad6577f4b8779f84638040f2a3fd2` | yes |
| `tests/proofs/test_b_bars.py` | `141904f9c9a8304aa5befd052c20e2fa771250c5eefedf03a7bd63ea0c7e99b4` | yes |
| `tests/proofs/test_b_decision_record.py` | `7c009bf3ec5bd767d2810c5166330be8616d6b473ad8bb4c61f46a0195d8b71f` | yes |
| `tests/proofs/test_b_entry_charge_ledger.py` | `a64783815f19194f4c3cf58011492940b15ac0a4c4f85d48d3697f4cedab5712` | yes |
| `tests/proofs/test_b_entry_facade_sdk.py` | `684c0e26454ab6ded2dd712e38b83ae2c32dad1ef3bd1742be5163df0ff6cce1` | yes |
| `tests/proofs/test_b_entry_pacer.py` | `852fff224fd74cd9f43bffd8e50951b3b85305d482f4f5c5937b1d074c431c8f` | yes |
| `tests/proofs/test_b_entry_partial_reserve.py` | `d92219e94a96b2c4c6e8826b5e83b6ccd1d961e88c68b34da8d134fbbe0f90e4` | yes |
| `tests/proofs/test_b_entry_reserve_review.py` | `308b7bfe841451fdf90c5288c44f872202026c914071b2601df0c518fe4bb688` | yes |
| `tests/proofs/test_b_entry_sdk.py` | `e6f4e9d5727dccf1bf4e7d9b453b15336e7ec0ee1047e99c62ffd9ed759bffde` | yes |
| `tests/proofs/test_b_lifecycle.py` | `a11c42427caec5146a570ce5dac3acf3addb65dd3866e06cda0c00d786472b8b` | yes |
| `tests/proofs/test_b_lifecycle_entry.py` | `e498c616cd9a0221515f30f458cd7858cdc94ede927557d549ab11096a548a31` | yes |
| `tests/proofs/test_b_lifecycle_protect.py` | `ed95949607d7d31d14cbdbe8e0ece88376bd1d625eabf9c29508b4f74b1759db` | yes |
| `tests/proofs/test_b_lifecycle_protect_delta.py` | `88ea52ee4925d3a8c60aa43aef3d117e40504661129a6c582fe21e9ed1f11ec2` | yes |
| `tests/proofs/test_b_lifecycle_protect_review.py` | `4320a944718b51c466375fa7568e6ad1a6ecaa5bd069486f64e6fda8f7ad582c` | yes |
| `tests/proofs/test_b_lifecycle_width.py` | `7fed63e9da2b6694417ab3b10a81d73040b0f0dc09e3369045b18a3ca8c5b9b8` | yes |
| `tests/proofs/test_b_lifecycle_width_delta.py` | `339e9b079f9a11d660f5893d4689575745c139bd400994cc229cb19375070d42` | yes |
| `tests/proofs/test_b_lifecycle_width_review.py` | `6edb8d99883d8bea2241cb0c5e8d57c49bc427b0411d9adfb1c0bf201ba0a963` | yes |
| `tests/proofs/test_b_observability.py` | `e1d29f20b3f89d223f6dc464af318dee977215e293e4991eec3f9b7e68258072` | yes |
| `tests/proofs/test_b_outside_rth_feed.py` | `95ab6995faa8affbf38a7da76f45b0910477d255c3de516936e65f411f879b7d` | yes |
| `tests/proofs/test_b_protect_cancel.py` | `a96da9e0bcf89ba6dd9c07ac725cef552719d022267a8a831c58f72c586b6444` | yes |
| `tests/proofs/test_b_protect_cells.py` | `3ca4f45150af1d015cac74cda1af377c6feb2f99d31fa2fd95d809f40d68dd79` | yes |
| `tests/proofs/test_b_protect_client.py` | `df93462840238444a314355344e6c7929dcda2444c994b939d469b5b1c9b63c4` | yes |
| `tests/proofs/test_b_protect_fix1.py` | `7e16ade2819ae168c6bd4908c6dd7165e142342d25d2b84d351640b5fd4af250` | yes |
| `tests/proofs/test_b_protect_price.py` | `26aee548d9058eba355b5db8772f4ff6e2b89b7e3829c6958f4af6666538ff22` | yes |
| `tests/proofs/test_b_protect_single.py` | `dc6b39a6d2102a238a50c4db3ce28a00f5f3b0443975a03873fcd1b4eafceb9e` | yes |
| `tests/proofs/test_b_protect_venue.py` | `d7ebe88ece9c29cb2719a576596305ac4e5487b50c368cfd1d135f53445520e1` | yes |
| `tests/proofs/test_b_protector.py` | `a684480f90ce6666ee3012070a5777692f6aad082a5e2fd918520d4f5dd6ff39` | yes |
| `tests/proofs/test_b_protector_main.py` | `e3536c0b28ae32d660f8a20aa79104cadd08ad238d7e5071f80c12584fee4249` | yes |
| `tests/proofs/test_b_protector_outside_rth.py` | `e57d37abc4d7b46906a67d7b9d9dea90f3e56f77beabd92471f6961ac666d377` | yes |
| `tests/proofs/test_b_protector_schedule.py` | `dd043126ee390bb55ef827bbe9fdf826eed25e93adaefd1f61671c3374b544a9` | yes |
| `tests/proofs/test_b_tape_probe_properties.py` | `3cf8dbeaabdbae3d75f4f358edbc3a09b7847bd980305395bbcd0fc495f4c333` | yes |
| `tests/proofs/test_b_tape_probe_regressions.py` | `be2fad208e6cad6959703cca1d0fe14b90e77bf0086d9b76e3ee662b887f362e` | yes |
| `tests/proofs/test_b_tape_review.py` | `b4b8c3971ba2e1a304447dec2f7a0f8cde2c2a8063499ca9aef12c9b0cce9082` | yes |
| `tests/proofs/test_b_width.py` | `265ce102903ea40b9c6aa59fe0d1bd16cdf684427740c071358e2a6c1fc877fb` | yes |
| `tests/proofs/test_baseline_builder.py` | `a2cba586a7a62639ab0a57dbbe254fb9a797e4ffd0ca59613d938bcd8cd6ee84` | yes |
| `tests/proofs/test_baseline_nanos_engine_equality.py` | `6378d93db5a73523ac4994c0590ce3ba28869b424eaad11ef5a53ceb18fad232` | yes |
| `tests/proofs/test_capacity_actor.py` | `f440632b54760252ea689f35dffaa96cf9c6a3a3e8c3102ad42faa04db58f010` | yes |
| `tests/proofs/test_data_trades.py` | `edbf555ac8ebca496f16b8e8b8199f2bc99b1e34d42233e2451cad44e030892b` | yes |
| `tests/proofs/test_j5_cadence.py` | `a56b19bff146080f93d0ea3fcece44bc554fa8ac5e62e4eac99d143113bbc836` | yes |
| `tests/proofs/test_k2_registry.py` | `34e599191ea3d93a625768d5d4f49fff211cafb6ff3233de33b079ea6e4e4879` | yes |
| `tests/proofs/test_outside_rth.py` | `8dbe56174f79ab372eda50fe50dcfb369d6332de15ebf861cadd6078bb0e1f77` | yes |
| `tests/proofs/test_overnight_baseline.py` | `c283ff8817758ff780927fc2dfdcbd14a07bc1817b34da8e5c4fd28e9c0a5405` | yes |
| `tests/proofs/test_paper_config.py` | `e1f1b1a7925937fa3a84b421a6941d00b163c45911be843168af6bf859f4166a` | yes |
| `tests/proofs/test_paper_trailing.py` | `1064f182afdda5a3bf419d16cb55f03ca643f0a00bcf28f37b1aaabc30b4ad1e` | yes |
| `tests/proofs/test_pending_delta.py` | `7cd5469c04ea19c64f794249e39d7d7cae17be832795e68a75ff00729213d8b5` | yes |
| `tests/proofs/test_rank_gate.py` | `9f2a77d7e684c8da7ee55de0ed4b8c73318bbad7fead8ba9b891f55c84734427` | yes |
| `tests/proofs/test_ranking.py` | `75476ce4571cfc1621e0c8b616f2b7bd9d1afa634b0c71ba3bf8fd1e17520777` | yes |
| `tests/proofs/test_ranking_actor.py` | `b329acb9ffbd3d969134aace659500f15efd14dcc4232b7874d89517de6d930f` | yes |
| `tests/proofs/test_ranking_actor_evidence.py` | `798cdd8ed0423ed18174bb7e27820d15702467970a224e8fd7b4c306bd315917` | yes |
| `tests/proofs/test_ranking_actor_status.py` | `eca404d380cbff3819dd5e41c770938021efcbfcbb7057b75321f29513db0ad8` | yes |
| `tests/proofs/test_ranking_amendment_identity.py` | `ed4d302d8bdcdc9d4c1011191078d63a9b89b887688fa6aec3a9cd8d5ae4fe4a` | yes |
| `tests/proofs/test_ranking_amendments.py` | `b594daf165d017dcd2904b35d3eec7af5aeee6911b938be005234b449bef4946` | yes |
| `tests/proofs/test_ranking_boundary_gap.py` | `e610d964ca26c7113783168c3674bf5a31552a1a775fed342a08c2078682ce52` | yes |
| `tests/proofs/test_ranking_consumer.py` | `366c3b032cfe63ea8c444708be7c80c196772dfec15314d72f87a8628ae36756` | yes |
| `tests/proofs/test_ranking_gap_order.py` | `e921732a43d83717bd09eee123df06d8ec9ed78303278f4d5d8b7fc1f2cae2e6` | yes |
| `tests/proofs/test_ranking_heartbeat.py` | `dd320400d9f34c870c206069f7cbba6ca55c530c7c8db5496503f7d818ab5695` | yes |
| `tests/proofs/test_ranking_node_host.py` | `fb16e0027f0368a1146834cdbdbf5c96ab1aa1ad49df73eacb44e08735235bb9` | yes |
| `tests/proofs/test_ranking_node_rollover.py` | `4d12a41a1eaf4a734ea6816a5924d2d703262817fb7ff8d5c98edaa6b416f2aa` | yes |
| `tests/proofs/test_ranking_node_session.py` | `e41d6e4f1c46a9658816fc94683e8c8010835598e5f073d97beb0e9f0a59c2ea` | yes |
| `tests/proofs/test_review_l2_partial.py` | `839ac8e260d05cea921ea51a2086365e79e2a04a5bcaab511422fdb193a33508` | yes |
| `tests/proofs/test_review_races.py` | `7f5df789dc91a00ce84e252d1a04ee13d0951f3e8a82d0a2fa72b623c09ce41d` | yes |
| `tests/proofs/test_review_state_edges.py` | `462dbe29a5ca0789e6eb8777b1ba242c862db452cb0f6aca8023e3986e960c8c` | yes |
| `tests/proofs/test_surge.py` | `13e14df428dc28dc3e96d0e517c1f3a4ead6ff0c1c1f3257a524df4e3a9ea24a` | yes |
| `tests/proofs/test_tradingview.py` | `9c6a9c04133bd9462d0a3b784e2339ca8ce958fe72079f6cfaf244aca6ea28e6` | yes |
| `tests/proofs/test_tradingview_account.py` | `f500eb1e038ca3b35d27469d6ef9aac84a5285213d34c74c3f369df1975d4adb` | yes |
| `tests/proofs/test_tradingview_bridge.py` | `5d1b9a2ade9b24247bf3add7203c6f3b385630c429c084293ce86203b446cb5d` | yes |
| `tests/proofs/test_tradingview_display_bounds.py` | `a5d2ab88e2fc0d68fd65414271e8a66d7d0ab0797bc7f44470585d3123960351` | yes |
| `tests/proofs/test_tradingview_host.py` | `c4b62802755f787d3b6d071a2eb0c06743f8f46b3c0456385c2390ad1463f52a` | yes |
| `tests/proofs/test_tradingview_loop.py` | `7fc2b942fdf8d76e106abd7c4173bce1800c9966fb7127b7ab01cf35b4072b0d` | yes |
| `tests/proofs/test_tradingview_prestage.py` | `8f571f8d7bbf0daf3acc8837f630ca2f703af9b0c8dc91b35cc99ad75fdd094c` | yes |
| `tests/proofs/test_tradingview_prestage_checkpoint.py` | `711699b8d8d010f25201989ae870442727c2459ae108850ab2fb1f19072d8979` | yes |
| `tests/proofs/test_tradingview_prestage_clock_precision.py` | `d49a8a88d06fdee43ad815d63cbb6812a0047774ea4e56c20543f7017efeea32` | yes |
| `tests/proofs/test_tradingview_prestage_followups.py` | `2fa4adccda8d9591f4fbbe4402057c5a01e6cc3d93a4271029f690334bc9a07d` | yes |
| `tests/proofs/test_tradingview_prestage_incremental.py` | `7e8ddecf958f912ce2311e8394ce505227e3e8869a5c7ee724964c341ce603dd` | yes |
| `tests/proofs/test_tradingview_prestage_p2.py` | `fbfbafadf11b751f211448e7f204cc1108223b5d400b3a10b365e5d40c0b5614` | yes |
| `tests/proofs/test_tradingview_producer.py` | `9990551f501c1cedf92f5738fa75f5e6f3f465f5939cdef545d36176fa7024aa` | yes |
| `tests/proofs/test_tradingview_ranked_view.py` | `13b2c0f175414644668b609f6e75608871fa95788e997dd13e12731531d6b395` | yes |

The effective include inventory grows from 119 to 121 at this baseline: custody/test_b_attended_entry.py and custody/test_b_attended_stop.py are added. D copies 118 effective include files; test_tradingview_units.py, test_tradingview_listings.py and test_ranking_node_parameters.py belong to seat E. Three additional support files and eight fixture assets bring D's copied total to 129 byte-identical files. All 75 effective outside files are omitted. Seven test nodes are held explicitly in tests/conftest.py; all other imported test bodies remain runnable. No vendored chart library is copied or needed by these selected tests.

The collection hook follows [pytest collection-time skips](https://docs.pytest.org/en/stable/example/simple.html#control-skipping-of-tests-according-to-command-line-option), read 2026-11-11. Helpers remain importable even when their document assertions are held. Both Nasdaq fixtures remain byte-identical; seat E copies the same bytes, allowing identical additions to merge. The baseline owner-thread teardown is unchanged; a later GC-scope fix is not imported.

## Held tests

| Held test node | Reason |
| --- | --- |
| `tests/proofs/test_ranking.py::test_k2_preregistration_stays_frozen_and_not_run` | HELD: reads Sextant's research preregistration, outside the paper import |
| `tests/proofs/test_ranking.py::test_k3_preregistration_stays_frozen_and_not_run` | HELD: reads Sextant's research preregistration, outside the paper import |
| `tests/proofs/test_ranking.py::test_successor_n1_preregistration_stays_frozen_and_not_run` | HELD: reads Sextant's research preregistration, outside the paper import |
| `tests/proofs/test_ranking_heartbeat.py::test_the_entry_age_authority_stays_unavailable` | HELD: reads Sextant's manifest and ranking ADR, replaced by the landscape |
| `tests/proofs/test_tradingview.py::test_preset_roster_keeps_managed_tickets_plain_limit` | HELD: reads Sextant's mixed-topic TradingView manual, including IBKR |
| `tests/proofs/test_tradingview.py::test_preset_import_card_matches_the_reviewed_manifest_hashes` | HELD: reads the owner import card in Sextant's excluded TradingView manual |
| `tests/proofs/test_tradingview_prestage_followups.py::test_the_3a_arrival_note_is_gone_once_the_loop_ships` | HELD: reads Sextant's manifest and excluded TradingView manual |

## Integration verification

C's source import `960ae0b0ed8ec89e799e3df6d8e9747fda141bd2` was merged with `--no-ff`. All 129 copies remained identical. Serial `test_surge.py` smoke: 24 passed. Four-worker proofs: 1 failed, 2244 passed, 11 skipped, 9 xfailed, 79 warnings in 233.04s. The sole failure reads E-owned `scripts/keys.sh` from `test_tradingview.py::test_keys_registers_the_tradingview_login_sealed_name`; keep this assertion runnable. The lead accepted that expected failure for this local hand-back and requires landing C, then E, then D. After C/E land, merge main and rerun the full proofs and one four-worker ci-local; no ci-local has run in D yet. Baseline PyClock/PyLogger unsendable-drop warnings are recorded; the lead ruled that warnings alone do not stop the cut. No independent review or attended-run approval is claimed.
