import scale.olm.common as common
import os
import json
from pathlib import Path
import os
import copy
import shutil
import numpy as np
import subprocess


def archive(model):
    """Build an ORIGEN reactor archive in HDF5 format."""
    archive_file = model["archive_file"]
    config_file = model["work_dir"] + os.path.sep + "generate.json"

    # Load the permuation data
    with open(config_file, "r") as f:
        data = json.load(f)

    assem_tag = "assembly_type={:s}".format(model["name"])
    lib_paths = []

    # Tag each permutation's libraries
    for perm in data["perms"]:
        perm_dir = Path(perm["file"]).parent
        perm_name = Path(perm["file"]).stem
        statevars = perm["state"]
        lib_path = os.path.join(perm_dir, perm_name + ".system.f33")
        lib_paths.append(lib_path)
        common.logger.info(f"Now tagging {lib_path}")

        ts = ",".join(key + "=" + str(value) for key, value in statevars.items())
        try:
            subprocess.run(
                [
                    model["obiwan"],
                    "tag",
                    lib_path,
                    f"-interptags={ts}",
                    f"-idtags={assem_tag}",
                ],
                capture_output=True,
                check=True,
            )
        except subprocess.SubprocessError as error:
            print(error)
            print("OBIWAN library tagging failed; cannot build archive")

    to_consolidate = " ".join(lib for lib in lib_paths)
    common.logger.info(f"Building archive at {archive_file} ... ")
    try:
        subprocess.run(
            [
                model["obiwan"],
                "convert",
                "-format=hdf5",
                "-name={archive_file}",
                to_consolidate,
            ],
            check=True,
        )
    except subprocess.SubprocessError as error:
        print(error)
        print("OBIWAN library conversion to archive format failed")

    return {"archive_file": archive_file}


def generated_thinned_list(thin_factor, burnup_list):
    thinned_list = list()
    rm = 0.0
    j = 0
    for bu in burnup_list:
        if j == 0 or j == len(burnup_list) - 1:
            p = True
        elif rm - thin_factor > 0.0:
            p = True
        else:
            p = False
        if p:
            thinned_list.append(bu)
            rm = 0.0
        rm += 1.0
        j += 1
    return thinned_list


def __arpdata_txt_input_desc(fuel_type, suffix, dim_map, thin_factor):
    """Return a simple input description for arpdata.txt"""

    dim_map_str = ""
    for d in dim_map:
        dim_map_str += "input({}) -> arp({})  ".format(dim_map[d], d)
    return common.rst_table(
        "Input data for arpdata.txt build type",
        [25, 25, 50],
        1,
        [
            ["input", "value", "description"],
            ["fuel_type", f"{fuel_type}", "ARP fuel type (UOX/MOX)"],
            ["suffix", f"{suffix}", "suffix to search for matching library"],
            [
                "dim_map",
                f"{dim_map_str}",
                "simple map of input variable names to interpolation variables names",
            ],
            [
                "thin_factor",
                f"{thin_factor}",
                "simple factor to use for thinning (0.0 do nothing, 1.0 remove every other burnup, etc.)",
            ],
        ],
    )


def arpdata_txt(model, fuel_type, suffix, dim_map, thin_factor):
    """Build an ORIGEN reactor library in arpdata.txt format."""

    # Get working directory.
    work_dir = Path(model["work_dir"])

    # Get list of files by using the generate.json output and changing the
    # suffix to the expected library file.
    generate_json = work_dir / "generate.json"
    with open(generate_json, "r") as f:
        generate = json.load(f)
    lib_list = list()
    input_list = list()
    for perm in generate["perms"]:
        input_list.append(perm["file"])
        # Convert from .inp to expected suffix.
        lib = work_dir / Path(perm["file"])
        lib = lib.with_suffix(suffix)
        if not lib.exists():
            common.logger.error(f"library file={lib} does not exist!")
            raise ValueError
        lib_list.append(lib)

    # Initialize library info data structure.
    arpinfo = common.ArpInfo()
    if fuel_type == "UOX":
        key_e = dim_map["enrichment"]
        key_m = dim_map["mod_dens"]
        enrichment_list = []
        mod_dens_list = []
        burnup_list = []

        perms = generate["perms"]
        for i in range(len(perms)):
            # Get library file and other convenience variables.
            lib = lib_list[i]
            input = perm["file"]
            output = Path(input).with_suffix(".out")
            perms[i]["output"] = str(output)
            perms[i]["lib"] = str(lib)
            perm = perms[i]
            state = perm["state"]

            # Accumulate the three dimensions:
            e = state[key_e]
            enrichment_list.append(e)

            m = state[key_m]
            mod_dens_list.append(m)

            bu = common.parse_burnups_from_triton_output(work_dir / output)

            if len(burnup_list) > 0 and not np.array_equal(burnup_list, bu):
                raise ValueError(
                    f"library file={lib} burnups deviated from previous list!"
                )
            burnup_list = bu

        arpinfo.init_uox(model["name"], lib_list, enrichment_list, mod_dens_list)
        arpinfo.burnup_list = burnup_list

    else:
        raise ValueError("only fuel_type==UOX is supported right now")

    # Set new canonical file names.
    arpinfo.set_canonical_filenames(".h5")

    # Generate burnup string.
    bu_str = ",".join([str(bu) for bu in arpinfo.burnup_list])
    idtags = "assembly_type={:s},fuel_type={:s}".format(arpinfo.name, arpinfo.fuel_type)

    # Generate thinned burnup list.
    thinned_list = generated_thinned_list(thin_factor, arpinfo.burnup_list)
    arpinfo.burnup_list = thinned_list
    thin_bu_str = ",".join([str(bu) for bu in thinned_list])
    common.logger.info(
        f"replacing burnup list {bu_str} with {thin_bu_str} based on thin_factor={thin_factor}"
    )

    # Create the arplibs directory and create data files inside.
    d = Path(work_dir) / "arplibs"
    if d.exists():
        shutil.rmtree(d)
    os.mkdir(d)
    perms = list()
    for i in range(len(lib_list)):
        old_lib = lib_list[i]
        tmp = d / "tmp"
        tmp.mkdir(parents=True, exist_ok=True)
        old_lib2 = tmp / lib_list[i].name
        new_lib = d / arpinfo.get_lib_by_index(i)

        obiwan = model["obiwan"]
        common.logger.info(f"using OBIWAN to convert {old_lib.name} to {new_lib.name}")
        common.logger.info(f"copying original library {old_lib} to {old_lib2}")
        shutil.copyfile(old_lib, old_lib2)

        # Set burnups on file using obiwan (should only be necessary in earlier SCALE versions).
        common.run_command(f"{obiwan} convert -i -setbu='[{bu_str}]' {old_lib2}")
        bad_local = Path(old_lib2.with_suffix(".f33").name)
        if bad_local.exists():
            common.logger.warning(f"fixup: moving local={bad_local} to {old_lib2}")
            shutil.move(bad_local, old_lib2)

        # Perform thinning.
        if bu_str != thin_bu_str:
            common.run_command(
                f"{obiwan} convert -i -thin=1 -tvals='[{thin_bu_str}]' {old_lib2}",
                check_return_code=False,
            )
            if bad_local.exists():
                common.logger.warning(f"fixup: moving local={bad_local} to {old_lib2}")
                shutil.move(bad_local, old_lib2)

        # Set tags on file using obiwan.
        interptags = arpinfo.interptags_by_index(i)
        common.run_command(
            f"{obiwan} tag -interptags='{interptags}' -idtags='{idtags}' {old_lib2}"
        )
        # Convert to HDF5 and move to arplibs.
        common.run_command(
            f"{obiwan} convert -format=hdf5 -type=f33 {old_lib2} -dir={tmp}"
        )
        shutil.move(old_lib2.with_suffix(".h5"), new_lib)

        # Save relevant permutation data in a list.
        perms.append({"input": input_list[i], **arpinfo.interpvars_by_index(i)})

    # Remove temporary files.
    shutil.rmtree(tmp)

    # Write arpdata.txt.
    arpdata_txt = work_dir / "arpdata.txt"
    common.logger.info(f"Building arpdata.txt at {arpdata_txt} ... ")
    with open(arpdata_txt, "w") as f:
        f.write(arpinfo.get_arpdata())

    return {
        "archive_file": "arpdata.txt:" + arpinfo.name,
        "perms": perms,
        "work_dir": str(work_dir),
        "input_desc": __arpdata_txt_input_desc(fuel_type, suffix, dim_map, thin_factor),
    }
