import scale.olm.internal as internal
import scale.olm.core as core
import os
import json
from pathlib import Path
import os
import copy
import shutil
import numpy as np
import subprocess
import datetime
from typing import Literal

__all__ = ["arpdata_txt"]

_TYPE_ARPDATA_TXT = "scale.olm.assemble:arpdata_txt"


def _schema_arpdata_txt(with_state: bool = False):
    _schema = internal._infer_schema(_TYPE_ARPDATA_TXT, with_state=with_state)
    return _schema


def _test_args_arpdata_txt(with_state: bool = False):
    return {
        "_type": _TYPE_ARPDATA_TXT,
        "dry_run": False,
        "fuel_type": "UOX",
        "dim_map": {"mod_dens": "mod_dens", "enrichment": "enrichment"},
    }


def arpdata_txt(
    fuel_type: str,
    dim_map: dict,
    keep_every: int,
    _model: dict = {},
    _env: dict = {},
    dry_run: bool = False,
    _type: Literal[_TYPE_ARPDATA_TXT] = None,
):
    """Build an ORIGEN reactor library in arpdata.txt format.

    Args:
        fuel_type: Which type of fuel: UOX/MOX.

        dim_map: arpdata.txt requires specially named dimensions. These may exist in the
                 state or you may need to map them from the state variables.

                 if fuel_type=='UOX', enrichment, mod_dens must be mapped to state variables
                 if fuel_type=='MOX', pu239_frac, pu_frac, mod_dens must be mapped to state variables


    """

    if dry_run:
        return {}

    # Get working directory.
    work_path = Path(_env["work_dir"])

    # Get library info data structure.
    arpinfo = _get_arpinfo(work_path, _model["name"], fuel_type, dim_map)

    # Generate thinned burnup list.
    thinned_burnup_list = _generate_thinned_burnup_list(keep_every, arpinfo.burnup_list)

    # Process libraries into their final places.
    archive_file, points = _process_libraries(
        _env["obiwan"], work_path, arpinfo, thinned_burnup_list
    )

    return {
        "archive_file": archive_file,
        "points": points,
        "work_dir": str(work_path),
        "date": datetime.datetime.utcnow().isoformat(" ", "minutes"),
        "space": arpinfo.get_space(),
    }


def archive(model):
    """Build an ORIGEN reactor library in HDF5 archive format.

    Args:
        model (dict): A dictionary containing the following keys:
            - archive_file (str): The path and filename of the reactor archive to be created.
            - work_dir (str): The path to the working directory.
            - name (str): The name of the reactor.
            - obiwan (str): The path to the OBIWAN executable.

    Returns:
        dict: relevant data on the result of creating an archive
    """
    archive_file = model["archive_file"]
    config_file = model["work_dir"] + os.path.sep + "generate.olm.json"

    # Load the permuation data
    with open(config_file, "r") as f:
        data = json.load(f)

    assem_tag = "assembly_type={:s}".format(model["name"])
    lib_paths = []

    # Tag each permutation's libraries
    for perm in data["perms"]:
        perm_dir = Path(perm["input_file"]).parent
        perm_name = Path(perm["input_file"]).stem
        statevars = perm["state"]
        lib_path = os.path.join(perm_dir, perm_name + ".system.f33")
        lib_paths.append(lib_path)
        internal.logger.debug(f"Now tagging {lib_path}")

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
            print("OBIWAN library tagging failed; cannot assemble archive")

    to_consolidate = " ".join(lib for lib in lib_paths)
    internal.logger.info(f"Building archive at {archive_file} ... ")
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


def _generate_thinned_burnup_list(keep_every, y_list, always_keep_ends=True):
    """Generate a thinned list using every point (1), every other point (2),
    every third point (3), etc."""

    if not keep_every > 0:
        raise ValueError(
            "The thinning parameter keep_every={keep_every} must be an integer >0!"
        )

    thinned_burnup_list = list()
    j = 0
    rm = 1
    for y in y_list:
        if always_keep_ends and (j == 0 or j == len(y_list) - 1):
            p = True
        elif rm >= keep_every:
            p = True
        else:
            p = False
        if p:
            thinned_burnup_list.append(y)
            rm = 0
        rm += 1
        j += 1
    return thinned_burnup_list


def _get_files(work_dir, suffix, perms):
    """Get list of files by using the generate.olm.json output and changing the suffix to the
    expected library file. Note this is in permutation order, not state space order."""

    file_list = list()
    for perm in perms:
        input = perm["input_file"]

        # Convert from .inp to expected suffix.
        lib = work_dir / Path(input)
        lib = lib.with_suffix(suffix)
        if not lib.exists():
            raise ValueError(f"library file={lib} does not exist!")

        output = work_dir / Path(input).with_suffix(".out")
        if not output.exists():
            raise ValueError(
                f"output file={output} does not exist! Maybe run was not complete successfully?"
            )

        file_list.append({"lib": lib, "output": output})

    return file_list


def _get_burnup_list(file_list):
    """Extract a burnup list from the output file and make sure they are all the same."""
    burnup_list = list()
    previous_output_file = ""
    for i in range(len(file_list)):
        output_file = file_list[i]["output"]
        bu = core.ScaleOutfile.parse_burnups_from_triton_output(output_file)

        if len(burnup_list) > 0 and not np.array_equal(burnup_list, bu):
            raise ValueError(
                f"Output file={output_file} burnups deviated from previous {previous_output_file}!"
            )
        burnup_list = bu
        previous_output_file = output_file

    return burnup_list


def _get_arpinfo_uox(name, perms, file_list, dim_map):
    """For UOX, get the relative ARP interpolation information."""

    # Get the names of the keys in the state.
    key_e = dim_map["enrichment"]
    key_m = dim_map["mod_dens"]

    # Build these lists for each permutation to use in init_uox below.
    enrichment_list = []
    mod_dens_list = []
    lib_list = []
    for i in range(len(perms)):
        # Get the interpolation variables from the state.
        state = perms[i]["state"]
        e = state[key_e]
        enrichment_list.append(e)
        m = state[key_m]
        mod_dens_list.append(m)

        # Get the library name.
        lib_list.append(file_list[i]["lib"])

    # Create and return arpinfo.
    arpinfo = core.ArpInfo()
    arpinfo.init_uox(name, lib_list, enrichment_list, mod_dens_list)
    return arpinfo


def _get_arpinfo_mox(name, perms, file_list, dim_map):
    """For MOX, get the relative ARP interpolation information."""

    # Get the names of the keys in the state.
    key_e = dim_map["pu239_frac"]
    key_p = dim_map["pu_frac"]
    key_m = dim_map["mod_dens"]

    # Build these lists for each permutation to use in init_uox below.
    pu239_frac_list = []
    pu_frac_list = []
    mod_dens_list = []
    lib_list = []
    for i in range(len(perms)):
        # Get the interpolation variables from the state.
        state = perms[i]["state"]
        e = state[key_e]
        pu239_frac_list.append(e)
        p = state[key_p]
        pu_frac_list.append(p)
        m = state[key_m]
        mod_dens_list.append(m)

        # Get the library name.
        lib_list.append(file_list[i]["lib"])

    # Create and return arpinfo.
    arpinfo = core.ArpInfo()
    arpinfo.init_mox(name, lib_list, pu239_frac_list, pu_frac_list, mod_dens_list)
    return arpinfo


def _get_arpinfo(work_dir, name, fuel_type, dim_map):
    """Populate the ArpInfo data."""

    # Get generate data which has permutations list with file names.
    generate_json = work_dir / "generate.olm.json"
    with open(generate_json, "r") as f:
        generate = json.load(f)
    perms = generate["perms"]

    # Get library,input,output in one place.
    suffix = ".system.f33"
    file_list = _get_files(work_dir, suffix, perms)

    # Initialize info based on fuel type.
    if fuel_type == "UOX":
        arpinfo = _get_arpinfo_uox(name, perms, file_list, dim_map)
    elif fuel_type == "MOX":
        arpinfo = _get_arpinfo_mox(name, perms, file_list, dim_map)
    else:
        raise ValueError(
            "Unknown fuel_type={fuel_type} (only MOX/UOX is supported right now)"
        )

    # Get the burnups.
    arpinfo.burnup_list = _get_burnup_list(file_list)

    # Set new canonical file names.
    arpinfo.set_canonical_filenames(".h5")

    return arpinfo


def _get_comp_system(ii_data):
    """Extract the following information from the inventory interface (ii) data."""

    x = ii_data["responses"]["system"]
    volume = x["volume"]
    amount_list = x["amount"][0]  # Initial amount
    data_map = ii_data["data"]["nuclides"]
    vh = x["nuclideVectorHash"]
    nuclide_list = ii_data["definitions"]["nuclideVectors"][vh]

    x = dict()
    total_mass = 0.0
    for i in range(len(nuclide_list)):
        name = nuclide_list[i]
        data = data_map[name]
        amount = amount_list[i]
        molar_mass = data["mass"]
        mass = amount * molar_mass
        total_mass += mass
        z = data["atomicNumber"]
        e = data["element"]
        m = data["isomericState"]
        a = data["massNumber"]
        mstr = ""
        if m >= 1:
            mstr = "m"
        elif m >= 2:
            mstr = "m" + str(m)
        eam = "{}{}{}".format(e.lower(), int(a), mstr)
        if z >= 92:
            x[eam] = amount * molar_mass

    comp = core.CompositionManager.calculate_hm_oxide_breakdown(x)
    comp["info"] = core.CompositionManager.approximate_hm_info(comp)
    comp["density"] = total_mass / volume

    return comp


def _process_libraries(obiwan, work_dir, arpinfo, thinned_burnup_list):
    """Process libraries with OBIWAN, including copying, thinning, setting tags, etc."""

    # Create the arplibs directory and clear data files inside.
    d = work_dir / "arplibs"
    if d.exists():
        shutil.rmtree(d)
    os.mkdir(d)

    # Generate burnup string.
    bu_str = ",".join([str(bu) for bu in arpinfo.burnup_list])

    # Generate idtags.
    idtags = "assembly_type={:s},fuel_type={:s}".format(arpinfo.name, arpinfo.fuel_type)

    # Generate burnup string for thin list.
    thin_bu_str = ",".join([str(bu) for bu in thinned_burnup_list])
    internal.logger.info("burnup thinning:", original_bu=bu_str, thinned_bu=thin_bu_str)
    arpinfo.burnup_list = thinned_burnup_list

    # Create a temporary directory for libraries in process.
    tmp = d / "tmp"
    tmp.mkdir(parents=True, exist_ok=True)

    # Get generate data which has permutations list with file names.
    generate_json = work_dir / "generate.olm.json"
    with open(generate_json, "r") as f:
        generate = json.load(f)
    perms = generate["perms"]

    # The case for the "system" in the f71.
    caseid = -2

    # Use obiwan to perform most of the processes.
    points = list()
    for i in range(arpinfo.num_libs()):
        new_lib = Path(arpinfo.get_lib_by_index(i))
        old_lib = Path(arpinfo.origin_lib_list[i])
        tmp_lib = tmp / old_lib.name
        internal.logger.debug(f"Copying original library {old_lib} to {tmp_lib}")
        shutil.copyfile(old_lib, tmp_lib)

        # Set burnups on file using obiwan (should only be necessary in earlier SCALE versions).
        internal.run_command(
            f"{obiwan} convert -i -setbu='[{bu_str}]' {tmp_lib}", echo=False
        )
        bad_local = Path(tmp_lib.with_suffix(".f33").name)
        if bad_local.exists():
            internal.logger.warning("Fixup: relocating local", file=str(bad_local))
            shutil.move(bad_local, tmp_lib)

        # Perform burnup thinning.
        if bu_str != thin_bu_str:
            internal.run_command(
                f"{obiwan} convert -i -thin=1 -tvals='[{thin_bu_str}]' {tmp_lib}",
                check_return_code=False,
                echo=False,
            )
            if bad_local.exists():
                internal.logger.warning("Fixup: relocating local", file=str(bad_local))
                shutil.move(bad_local, tmp_lib)

        # Set tags.
        interptags = arpinfo.interptags_by_index(i)
        internal.run_command(
            f"{obiwan} tag -interptags='{interptags}' -idtags='{idtags}' {tmp_lib}",
            echo=False,
        )

        # Convert to HDF5.
        internal.run_command(
            f"{obiwan} convert -format=hdf5 -type=f33 {tmp_lib} -dir={tmp}", echo=False
        )

        # Move the local library to the new proper place.
        new_lib = d / arpinfo.get_lib_by_index(i)
        shutil.move(tmp_lib.with_suffix(".h5"), new_lib)

        # Generate the system composition information from the system ii.json.
        k = arpinfo.get_perm_by_index(i)
        perm = perms[k]
        f71 = (work_dir / perm["input_file"]).with_suffix(".f71")
        text = internal.run_command(
            f"{obiwan} view -format=ii.json {f71} -cases='[{caseid}]'",
            echo=False,
        )

        # Load into data structure and rename.
        ii_json = new_lib.with_suffix(".ii.json")
        internal.logger.debug(f"Converting {f71} to {ii_json}")
        ii = json.loads(text)
        ii["responses"]["system"] = ii["responses"].pop(f"case({caseid})")
        with open(ii_json, "w") as f:
            f.write(json.dumps(ii, indent=4))

        # Get the special composition data structure.
        comp_system = _get_comp_system(ii)

        # Save relevant permutation data in a list.
        points.append(
            {
                "files": {
                    "origin": {
                        "lib": str(old_lib.relative_to(work_dir)),
                        "f71": str(f71.relative_to(work_dir)),
                    },
                    "lib": str(new_lib.relative_to(work_dir)),
                    "ii_json": str(ii_json.relative_to(work_dir)),
                },
                "comp": {
                    "system": comp_system,
                },
                "history": core.Obiwan.get_history_from_f71(obiwan, f71, caseid),
                "_": {"perm": perm},
                "_arpinfo": {
                    "interpvars": {**arpinfo.interpvars_by_index(i)},
                    "burnup_list": arpinfo.burnup_list,
                },
            }
        )

    # Remove temporary files.
    shutil.rmtree(tmp)

    # Write arpdata.txt.
    arpdata_txt = work_dir / "arpdata.txt"
    internal.logger.info(f"Writing arpdata.txt at {arpdata_txt} ... ")
    with open(arpdata_txt, "w") as f:
        f.write(arpinfo.get_arpdata())
    archive_file = "arpdata.txt:" + arpinfo.name

    return archive_file, points
