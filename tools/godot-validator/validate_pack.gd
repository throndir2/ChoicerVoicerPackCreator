extends SceneTree


func fail(message: String) -> void:
    printerr("VALIDATION ERROR: " + message)
    quit(1)


func _initialize() -> void:
    var arguments := OS.get_cmdline_user_args()
    if arguments.size() != 1:
        fail("usage: godot --headless --script validate_pack.gd -- <pack-directory>")
        return

    var pack_path := arguments[0].replace("\\", "/").trim_suffix("/")
    var directory := DirAccess.open(pack_path)
    if directory == null:
        fail("cannot open pack directory: " + pack_path)
        return

    for file_name in ["_pack_info.ini", "icon.png", "dub_video.ogv", "_backing_track.mp3"]:
        if not FileAccess.file_exists(pack_path.path_join(file_name)):
            fail("missing required file: " + file_name)
            return

    var pack_info := ConfigFile.new()
    var pack_error := pack_info.load(pack_path.path_join("_pack_info.ini"))
    if pack_error != OK:
        fail("Godot could not parse _pack_info.ini: error %d" % pack_error)
        return
    for key in ["title", "icon", "authors", "readme"]:
        if not pack_info.has_section_key("data", key):
            fail("_pack_info.ini is missing data/%s" % key)
            return
    if typeof(pack_info.get_value("data", "title")) != TYPE_STRING:
        fail("data/title is not a String")
        return
    if typeof(pack_info.get_value("data", "authors")) != TYPE_ARRAY:
        fail("data/authors is not an Array")
        return

    var metadata_files: Array[String] = []
    for file_name in directory.get_files():
        if file_name.get_extension().to_lower() == "txt":
            metadata_files.append(file_name)
    metadata_files.sort()
    if metadata_files.is_empty():
        fail("pack has no .txt clip metadata")
        return

    for file_name in metadata_files:
        var config := ConfigFile.new()
        var error := config.load(pack_path.path_join(file_name))
        if error != OK:
            fail("Godot could not parse %s: error %d" % [file_name, error])
            return
        for key in ["caption", "image", "dub_timestamps", "dub_characters"]:
            if not config.has_section_key("data", key):
                fail("%s is missing data/%s" % [file_name, key])
                return

        var caption: Variant = config.get_value("data", "caption")
        var image_name: Variant = config.get_value("data", "image")
        var timestamps: Variant = config.get_value("data", "dub_timestamps")
        var characters: Variant = config.get_value("data", "dub_characters")
        if typeof(caption) != TYPE_STRING or String(caption).is_empty():
            fail("%s has an invalid caption" % file_name)
            return
        if typeof(image_name) != TYPE_STRING or not FileAccess.file_exists(pack_path.path_join(String(image_name))):
            fail("%s references a missing image: %s" % [file_name, image_name])
            return
        if typeof(timestamps) != TYPE_ARRAY or timestamps.size() != 1 or typeof(timestamps[0]) != TYPE_FLOAT:
            fail("%s must contain exactly one floating-point dub timestamp" % file_name)
            return
        if timestamps[0] < 0.0:
            fail("%s has a negative dub timestamp" % file_name)
            return
        if typeof(characters) != TYPE_ARRAY or characters.is_empty():
            fail("%s has no dub characters" % file_name)
            return
        for character in characters:
            if typeof(character) != TYPE_STRING or String(character).is_empty():
                fail("%s has an invalid dub character" % file_name)
                return

    print("GODOT CONFIGFILE VALIDATION PASSED: %d clips" % metadata_files.size())
    quit(0)
